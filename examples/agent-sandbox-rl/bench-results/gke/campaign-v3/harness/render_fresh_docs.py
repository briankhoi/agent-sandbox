#!/usr/bin/env python3
"""Deterministically render all Step 3 fresh Markdown deliverables directly from `verified_metrics_summary.json`.

Generates from scratch (without reading or editing any untrusted prose):
  1. `campaign-v3/README.md` (for `briankhoi/agent-sandbox` `bench/rl-gke`, public-safe: `<GCP_PROJECT>`, 0 customer names)
  2. `agent-sandbox/performance_reports/11_05_2026_RESULTS.md` (for `ai-on-gke/sandbox-rl-performance` PR #5)
  3. `scratch/fresh_pr5_body.md` (PR #5 description matching `.github/PULL_REQUEST_TEMPLATE.md`)
  4. `GKE-13..16` replacement block in `~/agent-sandbox-notes/batch-primitives/handoff/learnings/gke-benchmarking.md`
"""
from __future__ import annotations

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUMMARY_JSON = os.path.join(SCRIPT_DIR, "verified_metrics_summary.json")


def _sp(a: float, b: float) -> str:
    """Speedup ratio a / b (when lower is better, a=per_claim, b=batch)."""
    return f"{a / b:.2f}×"


def _pt_by_c(suite_arm: dict, c: int) -> dict:
    for p in suite_arm["points"]:
        if p.get("concurrency", p.get("target_active")) == c:
            return p
    raise KeyError(f"Missing concurrency {c} in {suite_arm['file']}")


def _c3_3d_row(c3_3d: dict, mode: str, n: int) -> dict:
    for r in c3_3d["summary"]:
        if r["mode"] == mode and r["n"] == n:
            return r
    raise KeyError(f"Missing ({mode}, {n}) in C3-3d summary")


def render_campaign_v3_readme(s: dict) -> str:
    c2 = s["campaign_2"]
    c3 = s["campaign_3"]
    tot = s["campaign_3_totals"]
    c3_3d = c3["c3_3d_mc5_sdk_5000"]
    assert c3_3d is not None, "C3-3d must be present before rendering final docs!"

    # Shortcuts
    cs_pc = c3["c3_1a_cold_sbd_symmetric"]["per_claim"]["variants"][0]
    cs_ba = c3["c3_1a_cold_sbd_symmetric"]["batch64"]["variants"][0]
    u_pc = c3["c3_1a_anchor_unhydrated_sbd"]["per_claim"]["variants"][0]
    u_ba = c3["c3_1a_anchor_unhydrated_sbd"]["batch64"]["variants"][0]
    h_pc = c3["c3_1a_anchor_hydrated_exact_c2"]["per_claim"]["variants"][0]
    h_ba = c3["c3_1a_anchor_hydrated_exact_c2"]["batch64"]["variants"][0]
    c2_t1_ba = c2["phase3_t1_sbd_3000"]["naive"]["batch64"]["variants"][0]

    b_pc = c3["c3_1b_cold_gcfs_500"]["per_claim"]["variants"][0]
    b_ba = c3["c3_1b_cold_gcfs_500"]["batch64"]["variants"][0]

    w1_pc = c3["c3_1c_warm_ttfi_1x1000"]["per_claim"]
    w1_ba = c3["c3_1c_warm_ttfi_1x1000"]["batch64"]
    w2_pc = c3["c3_1c_warm_ttfi_200x5"]["per_claim"]
    w2_ba = c3["c3_1c_warm_ttfi_200x5"]["batch20"]

    d_pc = c3["c3_2_density"]["per_claim"]
    d_ba = c3["c3_2_density"]["batch64"]

    m4_pc = c3["c3_3a_mc5_warm4000"]["per_claim"]
    m4_ba = c3["c3_3a_mc5_warm4000"]["batch64"]

    gs_pc = c3["c3_3b_mc5_golden_small"]["per_claim"]
    gs_ba = c3["c3_3b_mc5_golden_small"]["batch20"]

    c8_pc = c3["c3_3c_mc5_cuj1_8000"]["per_claim"]["points"][0]
    c8_ba = c3["c3_3c_mc5_cuj1_8000"]["batch64"]["points"][0]

    b1000 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 1000)
    p1000 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 1000)
    b2500 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 2500)
    p2500 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 2500)
    b5000 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 5000)
    p5000 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 5000)
    d5000 = _c3_3d_row(c3_3d, "per_claim_sdk_delete_all", 5000)

    # Build 1x1000 and 200x5 rows
    rows_1x1000 = []
    for c_val in (100, 200, 500, 1000):
        pp = _pt_by_c(w1_pc, c_val)
        pb = _pt_by_c(w1_ba, c_val)
        rows_1x1000.append(
            f"| `{c_val}` | `per_claim` | `{pp['first_med_s']:.2f}s` | `{pp['p50_med_s']:.2f}s` | `{pp['avg_med_s']:.2f}s` | `{pp['p99_med_s']:.2f}s` | `{pp['max_med_s']:.2f}s` | `{pp['wire_p50_med_s']:.2f}s` | `{pp['first_cmd_p50_med_s']:.2f}s` | `{pp['acquire_wall_med_s']:.2f}s` (`{pp['claims_per_s_med']:.1f}/s`) | `{pp['release_wall_med_s']:.2f}s` | `{int(pp['held_open_sockets_med'])}` |\n"
            f"| **`{c_val}`** | **`batch64`** | **`{pb['first_med_s']:.2f}s`** | **`{pb['p50_med_s']:.2f}s`** (`{_sp(pp['p50_med_s'], pb['p50_med_s'])}`) | **`{pb['avg_med_s']:.2f}s`** (`{_sp(pp['avg_med_s'], pb['avg_med_s'])}`) | **`{pb['p99_med_s']:.2f}s`** (`{_sp(pp['p99_med_s'], pb['p99_med_s'])}`) | **`{pb['max_med_s']:.2f}s`** | `{pb['wire_p50_med_s']:.2f}s` | `{pb['first_cmd_p50_med_s']:.2f}s` | `{pb['acquire_wall_med_s']:.2f}s` (`{pb['claims_per_s_med']:.1f}/s`) | `{pb['release_wall_med_s']:.2f}s` | `{int(pb['held_open_sockets_med'])}` |"
        )

    rows_200x5 = []
    for c_val in (100, 200, 500, 1000):
        pp = _pt_by_c(w2_pc, c_val)
        pb = _pt_by_c(w2_ba, c_val)
        perr_p = f" *({pp['probe_err_all']} probe_err)*" if pp["probe_err_all"] else ""
        rows_200x5.append(
            f"| `{c_val}` | `per_claim` | `{pp['first_med_s']:.2f}s` | `{pp['p50_med_s']:.2f}s` | `{pp['avg_med_s']:.2f}s` | `{pp['p99_med_s']:.2f}s` | `{pp['max_med_s']:.2f}s` | `{pp['acquire_wall_med_s']:.2f}s` (`{pp['claims_per_s_med']:.1f}/s`){perr_p} | `{pp['release_wall_med_s']:.2f}s` | `{int(pp['held_open_sockets_med'])}` |\n"
            f"| **`{c_val}`** | **`batch20`** | **`{pb['first_med_s']:.2f}s`** | **`{pb['p50_med_s']:.2f}s`** (`{_sp(pp['p50_med_s'], pb['p50_med_s'])}`) | **`{pb['avg_med_s']:.2f}s`** (`{_sp(pp['avg_med_s'], pb['avg_med_s'])}`) | **`{pb['p99_med_s']:.2f}s`** (`{_sp(pp['p99_med_s'], pb['p99_med_s'])}`) | **`{pb['max_med_s']:.2f}s`** | **`{pb['acquire_wall_med_s']:.2f}s` (`{pb['claims_per_s_med']:.1f}/s`)** | `{pb['release_wall_med_s']:.2f}s` | **`{int(pb['held_open_sockets_med'])}`** (`{_sp(pp['held_open_sockets_med'], pb['held_open_sockets_med'])}` fewer) |"
        )

    rows_density = []
    for c_val in (640, 1600, 2400):
        pp = _pt_by_c(d_pc, c_val)
        pb = _pt_by_c(d_ba, c_val)
        rows_density.append(
            f"| `{c_val}` (`{pp['active_sandboxes_per_vcpu']}/vCPU`) | `per_claim` | `{pp['achieved_active']}/{c_val}` (`0` err) | `{pp['acquire_wall_s']:.2f}s` (`{pp['claims_per_s']:.1f}/s`) | `{pp['ttfi_s']['first']:.2f}s` / `{pp['ttfi_s']['p50']:.2f}s` / `{pp['ttfi_s']['p99']:.2f}s` / `{pp['ttfi_s']['max']:.2f}s` | `{pp['release_wall_s']:.2f}s` | `{pp['reap_to_zero_pods_s']:.2f}s` | `{pp['open_sockets_held']}` |\n"
            f"| **`{c_val}` (`{pb['active_sandboxes_per_vcpu']}/vCPU`)** | **`batch64`** | **`{pb['achieved_active']}/{c_val}` (`0` err)** | **`{pb['acquire_wall_s']:.2f}s` (`{pb['claims_per_s']:.1f}/s`, `{_sp(pp['acquire_wall_s'], pb['acquire_wall_s'])}`)** | **`{pb['ttfi_s']['first']:.2f}s` / `{pb['ttfi_s']['p50']:.2f}s` (`{_sp(pp['ttfi_s']['p50'], pb['ttfi_s']['p50'])}`) / `{pb['ttfi_s']['p99']:.2f}s` (`{_sp(pp['ttfi_s']['p99'], pb['ttfi_s']['p99'])}`) / `{pb['ttfi_s']['max']:.2f}s`** | `{pb['release_wall_s']:.2f}s` | `{pb['reap_to_zero_pods_s']:.2f}s` | **`{pb['open_sockets_held']}`** (`{_sp(pp['open_sockets_held'], pb['open_sockets_held'])}` fewer) |"
        )

    rows_mc4k = []
    for c_val in (1000, 2000, 4000):
        pp = _pt_by_c(m4_pc, c_val)
        pb = _pt_by_c(m4_ba, c_val)
        perr_b = f" *({pb['probe_err_all']} probe_err)*" if pb["probe_err_all"] else ""
        rows_mc4k.append(
            f"| `{c_val}` | `per_claim` | `{pp['first_med_s']:.2f}s` | `{pp['p50_med_s']:.2f}s` | `{pp['p99_med_s']:.2f}s` | `{pp['max_med_s']:.2f}s` | `{pp['acquire_wall_med_s']:.2f}s` (`{pp['claims_per_s_med']:.1f}/s`) | `{pp['release_wall_med_s']:.2f}s` | `{int(pp['held_open_sockets_med'])}` |\n"
            f"| **`{c_val}`** | **`batch64`** | **`{pb['first_med_s']:.2f}s`** | **`{pb['p50_med_s']:.2f}s`** (`{_sp(pp['p50_med_s'], pb['p50_med_s'])}`) | **`{pb['p99_med_s']:.2f}s`** (`{_sp(pp['p99_med_s'], pb['p99_med_s'])}`) | **`{pb['max_med_s']:.2f}s`** | **`{pb['acquire_wall_med_s']:.2f}s` (`{pb['claims_per_s_med']:.1f}/s`, `{_sp(pp['acquire_wall_med_s'], pb['acquire_wall_med_s'])}`){perr_b}** | `{pb['release_wall_med_s']:.2f}s` | `{int(pb['held_open_sockets_med'])}` |"
        )

    rows_3d = []
    for b_r, p_r in ((b1000, p1000), (b2500, p2500), (b5000, p5000)):
        n_v = b_r["n"]
        p_terr = f" *(+{p_r['term_err_all']} term_err)*" if p_r.get("term_err_all") else ""
        rows_3d.append(
            f"| `{n_v:,}` (`{n_v // 5}`/cluster) | `per_claim` concurrent `terminate()` (`1` rep) | `{p_r['ok_all']}/{n_v * p_r['repeats']}`{p_terr} | `{p_r['acquire_med_s']:.2f}s` (`{p_r['creates_per_s_med']:.1f}/s`) | `{p_r['release_api_med_s']:.2f}s` (`{p_r['release_api_per_s_med']:.1f}/s`, `{int(p_r['pods_alive_at_api_return_med'])}` alive) | `{p_r['claims_gone_med_s']:.2f}s` | `{p_r['pods_gone_med_s']:.2f}s` (`{p_r['pods_gone_per_s_med']:.1f} pods/s`) | `{int(p_r['socks_before_med'])} → {int(p_r['held_open_sockets_med'])}` |\n"
            f"| **`{n_v:,}` (`{n_v // 5}`/cluster)** | **`batch.release()` (`DELETE_COLLECTION`, `2` reps)** | **`{b_r['ok_all']}/{n_v * b_r['repeats']}`** | **`{b_r['acquire_med_s']:.2f}s` (`{b_r['creates_per_s_med']:.1f}/s`)** | **`{b_r['release_api_med_s']:.2f}s` (`{b_r['release_api_per_s_med']:.1f}/s`, `{int(b_r['pods_alive_at_api_return_med'])}` alive)** | **`{b_r['claims_gone_med_s']:.2f}s`** | **`{b_r['pods_gone_med_s']:.2f}s` (`{b_r['pods_gone_per_s_med']:.1f} pods/s`)** | **`{int(b_r['socks_before_med'])} → {int(b_r['held_open_sockets_med'])}`** |"
        )
    rows_3d.append(
        f"| `{d5000['n']:,}` (`1,000`/cluster) | `per_claim` SDK `delete_all()` (`1` rep) | `{d5000['ok_all']}/{d5000['n']}` | `{d5000['acquire_med_s']:.2f}s` (`{d5000['creates_per_s_med']:.1f}/s`) | `{d5000['release_api_med_s']:.2f}s` (`{d5000['release_api_per_s_med']:.1f}/s`, `{int(d5000['pods_alive_at_api_return_med'])}` alive) | `{d5000['claims_gone_med_s']:.2f}s` | `{d5000['pods_gone_med_s']:.2f}s` (`{d5000['pods_gone_per_s_med']:.1f} pods/s`) | `{int(d5000['socks_before_med'])} → {int(d5000['held_open_sockets_med'])}` |"
    )

    c3_losses = [x for x in s["batch_losses_and_tradeoffs"] if x["campaign"] == "C3"]
    loss_bullets = []
    for item in c3_losses:
        loss_bullets.append(
            f"- **`{item['suite']}` — `{item['metric']}`:** `per_claim = {json.dumps(item['per_claim'])}` vs `batch = {json.dumps(item['batch'])}` — {item['reason']}"
        )

    return f"""# GKE Campaign 3 — True TTFI, Compute Density & 5-Cluster Scale Benchmark Report

> **Status: ALL 21 BENCHMARK RUNS COMPLETE (`{tot['total_sandbox_operations']:,}` sandbox claim & rollout operations across `21` raw JSON artifacts; `0` claim errors across all `SandboxBatch` arms vs `{tot['total_claim_errors']}` `ConnectionTimeoutError` claim errors + `1` `terminate()` error on `per_claim` in `C3-3d`; `0` controller restarts; `{tot['total_probe_errors']}` transient exec `probe_err`s).**
> - **Single-Cluster True TTFI, Cold Pull, Symmetric First-Boot SBD & Packing Density (`asrl-c3-1`):** `14` runs complete (`C3-1a` symmetric first-boot cold SBD + symmetric hydrated `T1` SBD anchor + sequential hydration diagnostic, `C3-1b` `0%`-cached cold GCFS streaming, `C3-1c` `1×1,000` & `200×5` warm TTFI curves, `C3-2` `2.0 / 5.0 / 7.5` sandboxes/vCPU density sweep up to `2,400` concurrent gVisor pods).
> - **5-Cluster Fleet Horizontal Scale (`asrl-c3-1..5`, `50 × n2-standard-32` + `50 × 600 GB` SBD = `1,600` worker vCPUs):** `7` runs complete (`C3-3a` `4,000`-pod warm burst curve, `C3-3b` `8 × 512 = 4,096` concurrent RL jobs, `C3-3c` `8,000`-sandbox single-wave ceiling, and `C3-3d` `1,000 / 2,500 / 5,000`-sandbox pure Python SDK `AsyncSandboxBatch` claim + `DELETE_COLLECTION` release measuring both API return and full pod deletion).
>
> **Companion Artifacts in `campaign-v3/`:** [`raw/`](./raw/) (`21` JSONs) · [`smoke/`](./smoke/) (`14` smoke-gate JSONs) · [`telemetry/`](./telemetry/) (APF & run metadata) · [`manifests/`](./manifests/) · [`harness/`](./harness/)

---

## 1. Key Findings (Computed Directly From `raw/*.json`)

1. **5-Cluster `8,000`-Sandbox Single-Wave Ceiling (`C3-3c`):**
   - Across `5` regional GKE `1.37` clusters (`500` unique SWE-bench images, `8,000` standing warm gVisor pods), `batch64` acquired all `8,000` sandboxes in **`{c8_ba['acquire_wall_med_s']:.2f}s` (`{c8_ba['claims_per_s_med']:.1f} claims/s`)** vs `{c8_pc['acquire_wall_med_s']:.2f}s` (`{c8_pc['claims_per_s_med']:.1f} claims/s`) on `per_claim` (**`{_sp(c8_pc['acquire_wall_med_s'], c8_ba['acquire_wall_med_s'])}` faster**), lowered true end-to-end `TTFI` (`claim + first container exec`) `p50` from `{c8_pc['p50_med_s']:.2f}s → {c8_ba['p50_med_s']:.2f}s` (**`{_sp(c8_pc['p50_med_s'], c8_ba['p50_med_s'])}` faster**) and `p99` from `{c8_pc['p99_med_s']:.2f}s → {c8_ba['p99_med_s']:.2f}s` (**`{_sp(c8_pc['p99_med_s'], c8_ba['p99_med_s'])}` lower**), and reduced held client sockets **`{_sp(c8_pc['held_open_sockets_med'], c8_ba['held_open_sockets_med'])}` (`{int(c8_pc['held_open_sockets_med']):,} → {int(c8_ba['held_open_sockets_med']):,}`)** with `8,000 / 8,000` OK on both arms.
2. **5-Cluster Multi-Job Synchronous RL Step Barrier (`C3-3b`, `8` Concurrent Jobs × `512` Sandboxes = `4,096` Total):**
   - Driving `8` independent RL rollout jobs concurrently across `5` clusters (`4,096 / 4,096` OK, `0.00%` failure rate), `batch20` cut total multi-job wall time from `{gs_pc['all_jobs_wall_s']:.2f}s → {gs_ba['all_jobs_wall_s']:.2f}s` (**`{_sp(gs_pc['all_jobs_wall_s'], gs_ba['all_jobs_wall_s'])}` faster**), reduced the per-job `P100` step barrier from `{gs_pc['job_step_barrier_s']['max']:.2f}s → {gs_ba['job_step_barrier_s']['max']:.2f}s` (**`{_sp(gs_pc['job_step_barrier_s']['max'], gs_ba['job_step_barrier_s']['max'])}` faster**), and lowered aggregate `TTFI` `p50` `{gs_pc['aggregate_ttfi_s']['p50']:.2f}s → {gs_ba['aggregate_ttfi_s']['p50']:.2f}s` (**`{_sp(gs_pc['aggregate_ttfi_s']['p50'], gs_ba['aggregate_ttfi_s']['p50'])}`**) and `p99` `{gs_pc['aggregate_ttfi_s']['p99']:.2f}s → {gs_ba['aggregate_ttfi_s']['p99']:.2f}s` (**`{_sp(gs_pc['aggregate_ttfi_s']['p99'], gs_ba['aggregate_ttfi_s']['p99'])}`**).
3. **5-Cluster `5,000`-Pod Pure Python SDK Claim & Bulk Teardown — API Return vs Actual Pod Deletion (`C3-3d`):**
   - Across `6` consecutive `SandboxBatch` bursts (`1,000×2 + 2,500×2 + 5,000×2 = 17,000 / 17,000` claims), `batch` recorded **`0` errors**, acquiring `5,000` sandboxes in **`{b5000['acquire_med_s']:.2f}s` (`{b5000['creates_per_s_med']:.1f}/s`, `{_sp(p5000['acquire_med_s'], b5000['acquire_med_s'])}` faster** than `per_claim` `{p5000['acquire_med_s']:.2f}s`) and returning from `batch.release()` (`5` parallel `DELETE_COLLECTION` + `Lease` deletes = `10` RPCs) in **`{b5000['release_api_med_s']:.2f}s` (`{b5000['release_api_per_s_med']:,.1f}/s`)** vs **`{p5000['release_api_med_s']:.2f}s` (`{p5000['release_api_per_s_med']:,.1f}/s`, `{_sp(p5000['release_api_med_s'], b5000['release_api_med_s'])}` faster)** on `5,000` concurrent `terminate()` calls and **`{d5000['release_api_med_s']:.2f}s` (`{d5000['release_api_per_s_med']:,.1f}/s`, `{_sp(d5000['release_api_med_s'], b5000['release_api_med_s'])}` faster)** on `AsyncSandboxClient.delete_all()`. Because `kubernetes_asyncio.watch.Watch.stop()` closes response sockets on every per-claim watch, `per_claim` drained its keep-alive pool from `{int(p5000['socks_before_med'])} → {int(p5000['held_open_sockets_med'])}` sockets and hit `5/2,500` and `12–13/5,000` external GKE VIP `ConnectionTimeoutError`s, whereas `batch` kept its `{int(b5000['socks_before_med'])} → {int(b5000['held_open_sockets_med'])}` `ESTABLISHED` socket pool intact with `0` errors.
   - Measuring **actual pod termination convergence** (`pods_gone_s`: time from `release()` start until all `N` claimed `Pod` objects are absent across all `5` clusters, `terminationGracePeriodSeconds: 0`): when `batch.release()` returned at `{b5000['release_api_med_s']:.2f}s`, **`{int(b5000['pods_alive_at_api_return_med']):,} / 5,000` (`{b5000['pods_alive_at_api_return_med'] / 50.0:.1f}%`) claimed pods were still alive**; `SandboxClaim`s reached `0` at **`{b5000['claims_gone_med_s']:.2f}s`** (`{b5000['claims_gone_per_s_med']:,.1f}/s`), and all `5,000` claimed gVisor `Pod` objects reached `0` at **`{b5000['pods_gone_med_s']:.2f}s` (`{b5000['pods_gone_per_s_med']:.1f} pods/s`)** — **`{_sp(p5000['pods_gone_med_s'], b5000['pods_gone_med_s'])}` faster** than concurrent `terminate()` (`{p5000['pods_gone_med_s']:.2f}s`, `{p5000['pods_gone_per_s_med']:.1f} pods/s`) and **`{_sp(d5000['pods_gone_med_s'], b5000['pods_gone_med_s'])}` faster** than `delete_all()` (`{d5000['pods_gone_med_s']:.2f}s`, `{d5000['pods_gone_per_s_med']:.1f} pods/s`).
4. **True End-to-End Warm `TTFI` (`C3-1c`) & `0%`-Cached Cold GCFS Pull (`C3-1b`):**
   - **Single `1,000`-pod warm pool (`C3-1c`, 3-repeat medians):** True `TTFI` `p50` = **`{_pt_by_c(w1_ba, 100)['p50_med_s']:.2f}s`** (`c=100`), **`{_pt_by_c(w1_ba, 200)['p50_med_s']:.2f}s`** (`c=200`), **`{_pt_by_c(w1_ba, 500)['p50_med_s']:.2f}s`** (`c=500`), and **`{_pt_by_c(w1_ba, 1000)['p50_med_s']:.2f}s`** (`c=1,000`, **`{_sp(_pt_by_c(w1_pc, 1000)['p50_med_s'], _pt_by_c(w1_ba, 1000)['p50_med_s'])}` faster** than `per_claim` `{_pt_by_c(w1_pc, 1000)['p50_med_s']:.2f}s`).
   - **Multi-pool `200 × 5 = 1,000` warm fleet (`C3-1c`, 3-repeat medians):** At `c = 1,000`, `batch20` lowered `TTFI` `p50` `{_pt_by_c(w2_pc, 1000)['p50_med_s']:.2f}s → {_pt_by_c(w2_ba, 1000)['p50_med_s']:.2f}s` (**`{_sp(_pt_by_c(w2_pc, 1000)['p50_med_s'], _pt_by_c(w2_ba, 1000)['p50_med_s'])}` faster**) with `0` probe errors vs `4` transient probe errors on `per_claim`.
   - **Cold `0%`-cached GCFS streaming (`C3-1b`, fresh `gvisor-pool` recreated before each arm):** `batch64` completed `500` unique SWE-bench startups in **`{b_ba['wall_s']:.2f}s`** vs `{b_pc['wall_s']:.2f}s` on `per_claim` (**`{_sp(b_pc['wall_s'], b_ba['wall_s'])}` faster**; `TTFI p50` `{b_pc['paired_ttfi_s']['p50']:.2f}s → {b_ba['paired_ttfi_s']['p50']:.2f}s`).
5. **High-Density Packing Validation (`C3-2`, up to `7.5` Active Sandboxes / vCPU = `2,400` Concurrent gVisor Pods on `10` Nodes):**
   - Validated `640` (`2.0/vCPU`), `1,600` (`5.0/vCPU`), and `2,400` (`7.5/vCPU` = `240` active pods/node) simultaneous gVisor SWE-bench sandboxes with **`0` claim errors, `0` probe errors, `0` OOM kills, and `0` pod evictions**. At `2,400` active pods, `batch64` cut `TTFI` `p50` from `{_pt_by_c(d_pc, 2400)['ttfi_s']['p50']:.2f}s → {_pt_by_c(d_ba, 2400)['ttfi_s']['p50']:.2f}s` (**`{_sp(_pt_by_c(d_pc, 2400)['ttfi_s']['p50'], _pt_by_c(d_ba, 2400)['ttfi_s']['p50'])}`**) and `p99` from `{_pt_by_c(d_pc, 2400)['ttfi_s']['p99']:.2f}s → {_pt_by_c(d_ba, 2400)['ttfi_s']['p99']:.2f}s` (**`{_sp(_pt_by_c(d_pc, 2400)['ttfi_s']['p99'], _pt_by_c(d_ba, 2400)['ttfi_s']['p99'])}`**).

---

## 2. Cluster Topology, Guardrails & Code Provenance

| Parameter | Single-Cluster Suites (`asrl-c3-1`) | 5-Cluster Suites (`asrl-c3-1..5`) |
|---|---|---|
| **GKE Version & Control Plane** | Regional GKE `v1.37.1-gke.1697000` (`us-central1`), `3 × 16`-vCPU HA `kube-apiserver` (`go_sched_gomaxprocs_threads = 16` verified via `/metrics`, `1,449` nominal APF seats) | `5` regional GKE `v1.37.1-gke.1697000` clusters (`15` HA `kube-apiserver` replicas = `240` control-plane vCPUs, all verified at `gomaxprocs = 16`) |
| **Private Networking & Cloud NAT** | `--enable-private-nodes`, Cloud Router + Cloud NAT (`minPortsPerVm=4096`) | Shared VPC Cloud Router + Cloud NAT backed by **`8` static external NAT IPs** (`maxPortsPerVm=32768`, `tcpEstablishedIdleTimeout=1200s`) so private driver pods on `asrl-c3-1` reach `c2..c5` external control-plane VIPs without single-IP SYN throttling |
| **Controller (`ctrl-pool`)** | `2 × e2-standard-32` (`dedicated=ctrl:NoSchedule`), `agent-sandbox-controller@sha256:b0b3c550...`, `--sandbox-concurrent-workers=1000 --sandbox-claim-concurrent-workers=500 --sandbox-warm-pool-concurrent-workers=500 --sandbox-warm-pool-max-refill-rate=500 --kube-api-qps=-1 --api-connections=4 --separate-watch-connection=true --cache-label-selectors=true` | Identical per cluster (`10` `e2-standard-32` controller nodes total across `5` clusters) |
| **Worker Pool (`gvisor-pool`)** | `10 × e2-standard-32` (`320` vCPUs, `200 GB pd-balanced`, `0%` cache) in `C3-1b`; `10 × n2-standard-32` (`320` vCPUs, `maxPodsPerNode=256`, `gvisor`, `100 GB pd-standard` + `600 GB` `swebench-500-preload-600g` `CONTAINER_IMAGE_CACHE` SBD) in `C3-1a`, `C3-1c`, `C3-2` | `10 × n2-standard-32` (`100 GB pd-standard` + `600 GB` `swebench-500-preload-600g` SBD) per cluster = **`50` worker nodes (`1,600` worker vCPUs, `30 TB` preloaded SBD)** |
| **APF & Run Isolation** | 3-lane APF (`agent-sandbox-critical` `20` shares, `agent-sandbox-bulk` `10` shares, leader-election `leases` pinned to `global-default`); mandatory smoke gate + zero-object drain + **`20s–30s` settle (`SETTLE_S=20` in `C3-1b`, `25` in `C3-1c`, `30` in `C3-1a`, `C3-2`, and all `5`-cluster suites)** | Identical across all `5` clusters (`SETTLE_S=30` enforced before every launch) |

### Workstream B Driver SHA Provenance (`4018f0dd` → `bb443e71`)
- Campaign 2 used `agent-sandbox-controller@sha256:b0b3c550ad8eed1571c9494e3dc40b710aff724796cf4671fb1b7a5b959a3716` + Python SDK/RL commit `4018f0dd` (`asrl-driver@sha256:eb77db4f...`).
- Campaign 3 kept the **exact same controller image digest** (`@sha256:b0b3c550...`) and updated the Python driver image to commit **`bb443e71`** (`asrl-driver@sha256:6d0b37df8c80b092186d5c8ee66d13e646283d48a686301d6ece6c3532f2a8c3`), incorporating Workstream B correctness/hardening fixes on `feat/rl-batch-hardening`:
  - `Lease`-UID selector scoping and full-duration crash-reaper grace period (`22/N`–`23/N` restack);
  - Deadline-bounded warm-pool readiness precheck, `pod_annotations` propagation, shared `CoreV1Api` reuse, cold-start watchdog, parallel `acquire_batch` / `release_all` rollback on partial failure, auto-sized HTTP connection pools, `4,096` executor thread ceiling, and pool-resize attribute sync (`25/N`–`26/N`).
- **Empirical Performance Impact Verification (`C3-1a`):** None of those hardening fixes add extra RPCs on the steady-state hot path. Matched `T1` hydrated SBD `batch64` (`#42` `{h_ba['wall_s']:.2f}s`) reproduced Campaign 2 `#32` (`{c2_t1_ba['wall_s']:.2f}s`) within **`5.1%`** (`0` errors on both).

---

## 3. Single-Cluster Results (`asrl-c3-1`)

### 3A. `C3-1a` — `T1` Secondary Boot Disk Symmetric First-Boot Cold SBD (`#39–#40`) & Symmetric Hydrated SBD (`#41–#42`) (`500 × 6 = 3,000`, `c = 500`, `naive`, `probe='true'`)

*(For `#39` and `#40`, a brand-new `10 × n2-standard-32 + 600 GB SBD` `gvisor-pool` was deleted and recreated from scratch immediately before **each** arm with a non-SWE-bench smoke gate (`alpine/socat:1.8.0.1`), guaranteeing identical `0/500`-hydrated first-boot SBD block state. `#41` and `#42` ran back-to-back on fully hydrated SBD nodes. The initial sequential diagnostic pair (`545.64s` → `308.59s`, `tgs=-1`) that uncovered GCE's lazy GCS SBD hydration is included at the bottom for provenance.)*

| Footing & Cache State | Arm | `wall_s` (`s/task`) | `wait_pool_ready` `avg` / `max` | Paired `TTFI` (`First` / `P50` / `P99` / `Max`) | Open Sockets | Notes |
|---|---|---:|---:|---|---:|---|
| **Symmetric First-Boot Cold SBD** (`tgs=0, cow=True`, fresh SBD nodepool) | **`per_claim` (`#39`)** | **`{cs_pc['wall_s']:.2f}s` (`{cs_pc['s_per_task']:.4f}`)** | **`{cs_pc['wpr_avg_s']:.2f}s` / `{cs_pc['wpr_max_s']:.2f}s`** | **`{cs_pc['paired_ttfi_s']['first']:.2f}s / {cs_pc['paired_ttfi_s']['p50']:.2f}s / {cs_pc['paired_ttfi_s']['p99']:.2f}s / {cs_pc['paired_ttfi_s']['max']:.2f}s`** | `{cs_pc['open_sockets_after']}` | **`{_sp(cs_ba['wall_s'], cs_pc['wall_s'])}` faster wall than `#40`** (streaming `DELETE`s start `wp=1` GCS hydration refill before wave stragglers finish) |
| **Symmetric First-Boot Cold SBD** (`tgs=0, cow=True`, fresh SBD nodepool) | **`batch64` (`#40`)** | `{cs_ba['wall_s']:.2f}s` (`{cs_ba['s_per_task']:.4f}`) | `{cs_ba['wpr_avg_s']:.2f}s` / `{cs_ba['wpr_max_s']:.2f}s` | `{cs_ba['paired_ttfi_s']['first']:.2f}s / {cs_ba['paired_ttfi_s']['p50']:.2f}s / {cs_ba['paired_ttfi_s']['p99']:.2f}s / {cs_ba['paired_ttfi_s']['max']:.2f}s` | **`{cs_ba['open_sockets_after']}`** | **`{_sp(cs_pc['open_sockets_after'], cs_ba['open_sockets_after'])}` fewer sockets**; wave-boundary `DELETE_COLLECTION` delays refill until wave straggler finishes |
| **Symmetric Hydrated SBD** (`tgs=0, cow=True`, warm SBD blocks) | **`per_claim` (`#41`)** | **`{h_pc['wall_s']:.2f}s` (`{h_pc['s_per_task']:.4f}`)** | `{h_pc['wpr_avg_s']:.2f}s` / `{h_pc['wpr_max_s']:.2f}s` | `{h_pc['paired_ttfi_s']['first']:.2f}s / {h_pc['paired_ttfi_s']['p50']:.2f}s / {h_pc['paired_ttfi_s']['p99']:.2f}s / {h_pc['paired_ttfi_s']['max']:.2f}s` | `{h_pc['open_sockets_after']}` | **`{_sp(h_ba['wall_s'], h_pc['wall_s'])}` faster wall than `#42`** (streaming individual `DELETE`s overlap `wp=1` refill) |
| **Symmetric Hydrated SBD** (`tgs=0, cow=True`, warm SBD blocks) | **`batch64` (`#42`)** | `{h_ba['wall_s']:.2f}s` (`{h_ba['s_per_task']:.4f}`) | `{h_ba['wpr_avg_s']:.2f}s` / `{h_ba['wpr_max_s']:.2f}s` | **`{h_ba['paired_ttfi_s']['first']:.2f}s`** / `{h_ba['paired_ttfi_s']['p50']:.2f}s` / **`{h_ba['paired_ttfi_s']['p99']:.2f}s`** / `{h_ba['paired_ttfi_s']['max']:.2f}s` | **`{h_ba['open_sockets_after']}`** | Within `5.1%` of C2 `#32` (`{c2_t1_ba['wall_s']:.2f}s`); **`{_sp(h_pc['open_sockets_after'], h_ba['open_sockets_after'])}` fewer sockets** & lower `p99` |
| *Diagnostic — Sequential Run 1 (`tgs=-1, cow=False`, cold SBD)* | `per_claim` *(archived)* | `{u_pc['wall_s']:.2f}s` (`{u_pc['s_per_task']:.4f}`) | `{u_pc['wpr_avg_s']:.2f}s` / `{u_pc['wpr_max_s']:.2f}s` | `{u_pc['paired_ttfi_s']['first']:.2f}s / {u_pc['paired_ttfi_s']['p50']:.2f}s / {u_pc['paired_ttfi_s']['p99']:.2f}s / {u_pc['paired_ttfi_s']['max']:.2f}s` | `{u_pc['open_sockets_after']}` | Uncovered first-boot lazy GCS SBD hydration + 30s pod termination grace |
| *Diagnostic — Sequential Run 2 (`tgs=-1, cow=False`, partially hydrated)* | `batch64` *(archived)* | `{u_ba['wall_s']:.2f}s` (`{u_ba['s_per_task']:.4f}`) | `{u_ba['wpr_avg_s']:.2f}s` / `{u_ba['wpr_max_s']:.2f}s` | `{u_ba['paired_ttfi_s']['first']:.2f}s / {u_ba['paired_ttfi_s']['p50']:.2f}s / {u_ba['paired_ttfi_s']['p99']:.2f}s / {u_ba['paired_ttfi_s']['max']:.2f}s` | `{u_ba['open_sockets_after']}` | Not footing-comparable to Diagnostic Run 1 (inherited hydrated SBD blocks) |

### 3B. `C3-1b` — Truly Cold `0%`-Cached `T0` GCFS Image-Streaming (`500 × 1 = 500`, `c = 500`, `probe='python3 -V'`)

*(Fresh `10 × e2-standard-32` (`200 GB pd-balanced`) `gvisor-pool` deleted and recreated from scratch immediately prior to **each** arm so both arms paid true `0%`-cached GCFS pulls across all `500` images.)*

| Arm | `wall_s` (`tasks/s`) | `wait_pool_ready` `avg` / `max` | `env_ready_ok_s` `p50` / `p99` / `max` | Paired `TTFI` (`First` / `P50` / `P99` / `Max`) | Open Sockets |
|---|---:|---:|---|---:|---:|
| `per_claim` (`#37`) | `{b_pc['wall_s']:.2f}s` (`{b_pc['tasks_per_s']:.2f}/s`) | `{b_pc['wpr_avg_s']:.2f}s` / `{b_pc['wpr_max_s']:.2f}s` | `{b_pc['env_ready_ok_s']['p50']:.2f}s / {b_pc['env_ready_ok_s']['p99']:.2f}s / {b_pc['env_ready_ok_s']['max']:.2f}s` | `{b_pc['paired_ttfi_s']['first']:.2f}s / {b_pc['paired_ttfi_s']['p50']:.2f}s / {b_pc['paired_ttfi_s']['p99']:.2f}s / {b_pc['paired_ttfi_s']['max']:.2f}s` | `{b_pc['open_sockets_after']}` |
| **`batch64` (`#38`)** | **`{b_ba['wall_s']:.2f}s` (`{b_ba['tasks_per_s']:.2f}/s`, `{_sp(b_pc['wall_s'], b_ba['wall_s'])}`)** | **`{b_ba['wpr_avg_s']:.2f}s` / `{b_ba['wpr_max_s']:.2f}s`** | **`{b_ba['env_ready_ok_s']['p50']:.2f}s / {b_ba['env_ready_ok_s']['p99']:.2f}s / {b_ba['env_ready_ok_s']['max']:.2f}s`** | **`{b_ba['paired_ttfi_s']['first']:.2f}s / {b_ba['paired_ttfi_s']['p50']:.2f}s / {b_ba['paired_ttfi_s']['p99']:.2f}s / {b_ba['paired_ttfi_s']['max']:.2f}s`** | `{b_ba['open_sockets_after']}` *(higher concurrency overlap)* |

### 3C. `C3-1c` — True End-to-End Warm `TTFI` Burst Curves (`3` Repeats / Point = `5,400` Claims / Arm, Medians Shown)

#### Single `1,000`-Replica Warm Pool (`1 × 1,000`)

| `c` | Arm | `TTFI First` | `TTFI P50` | `TTFI Mean` | `TTFI P99` | `TTFI Max` | Wire `P50` | Exec `P50` | Acquire Wall (`Rate`) | Release Wall | Held Sockets |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows_1x1000)}

#### Multi-Pool `200 × 5 = 1,000` Warm Fleet (`per_claim` vs SDK Default `batch20`)

| `c` | Arm | `TTFI First` | `TTFI P50` | `TTFI Mean` | `TTFI P99` | `TTFI Max` | Acquire Wall (`Rate`) | Release Wall | Held Sockets |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows_200x5)}

### 3D. `C3-2` — Compute Packing Density Sweep (`10 × n2-standard-32` = `320` Worker vCPUs, `125m / 256Mi`, `0` OOMs / Evictions)

| Target Active (`Density`) | Arm | Active / Errors | Acquire Wall (`Rate`) | True `TTFI` (`First` / `P50` / `P99` / `Max`) | Release API (`s`) | Reap to `0` Pods (`s`) | Held Sockets |
|---|---|---|---:|---|---:|---:|---:|
{chr(10).join(rows_density)}

---

## 4. 5-Cluster Fleet Scale Results (`asrl-c3-1..5`, `50` Worker Nodes = `1,600` vCPUs)

### 4A. `C3-3a` — 5-Cluster `4,000`-Pod Warm Fleet Burst Curve (`2` Repeats / Point = `14,000` Claims / Arm, Medians Shown)

| `c` (`5` Clusters) | Arm | `TTFI First` | `TTFI P50` | `TTFI P99` | `TTFI Max` | Acquire Wall (`Rate`) | Release API (`s`) | Held Sockets |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows_mc4k)}

### 4B. `C3-3b` — 5-Cluster Concurrent Multi-Job RL Step (`8` Jobs × `512` Sandboxes = `4,096` Simultaneous Sandboxes)

| Metric | `per_claim` | **`batch20`** | Gain |
|---|---:|---:|---:|
| **Completed Sandboxes / Failure Rate** | `{gs_pc['total_ok']:,} / {gs_pc['total_concurrent_target']:,}` (`0.00%`) | **`{gs_ba['total_ok']:,} / {gs_ba['total_concurrent_target']:,}` (`0.00%`)** | `0` errors |
| **Warm Fleet Setup (`setup_s`)** | `{gs_pc['provision_warm_s']:.2f}s` | **`{gs_ba['provision_warm_s']:.2f}s`** | `{_sp(gs_pc['provision_warm_s'], gs_ba['provision_warm_s'])}` |
| **All-8-Jobs Total Wall (`all_jobs_wall_s`)** | `{gs_pc['all_jobs_wall_s']:.2f}s` (`{gs_pc['aggregate_goodput_tasks_per_s']:.2f}/s`) | **`{gs_ba['all_jobs_wall_s']:.2f}s` (`{gs_ba['aggregate_goodput_tasks_per_s']:.2f}/s`)** | **`{_sp(gs_pc['all_jobs_wall_s'], gs_ba['all_jobs_wall_s'])}` faster** |
| **Per-Job Step Barrier (`First` / `P50` / `P100` across 8 jobs)** | `{gs_pc['job_step_barrier_s']['first']:.2f}s / {gs_pc['job_step_barrier_s']['p50']:.2f}s / {gs_pc['job_step_barrier_s']['max']:.2f}s` | **`{gs_ba['job_step_barrier_s']['first']:.2f}s / {gs_ba['job_step_barrier_s']['p50']:.2f}s / {gs_ba['job_step_barrier_s']['max']:.2f}s`** | **`{_sp(gs_pc['job_step_barrier_s']['max'], gs_ba['job_step_barrier_s']['max'])}` lower `P100` barrier** |
| **Aggregate Wire Claim (`P50` / `P99` / `Max`, `n = 4,096`)** | `{gs_pc['aggregate_wire_claim_s']['p50']:.2f}s / {gs_pc['aggregate_wire_claim_s']['p99']:.2f}s / {gs_pc['aggregate_wire_claim_s']['max']:.2f}s` | **`{gs_ba['aggregate_wire_claim_s']['p50']:.2f}s / {gs_ba['aggregate_wire_claim_s']['p99']:.2f}s / {gs_ba['aggregate_wire_claim_s']['max']:.2f}s`** | **`{_sp(gs_pc['aggregate_wire_claim_s']['p99'], gs_ba['aggregate_wire_claim_s']['p99'])}` lower wire `p99`** |
| **Aggregate True `TTFI` (`First` / `P50` / `P99` / `Max`, `n = 4,096`)** | `{gs_pc['aggregate_ttfi_s']['first']:.2f}s / {gs_pc['aggregate_ttfi_s']['p50']:.2f}s / {gs_pc['aggregate_ttfi_s']['p99']:.2f}s / {gs_pc['aggregate_ttfi_s']['max']:.2f}s` | **`{gs_ba['aggregate_ttfi_s']['first']:.2f}s / {gs_ba['aggregate_ttfi_s']['p50']:.2f}s / {gs_ba['aggregate_ttfi_s']['p99']:.2f}s / {gs_ba['aggregate_ttfi_s']['max']:.2f}s`** | **`{_sp(gs_pc['aggregate_ttfi_s']['p50'], gs_ba['aggregate_ttfi_s']['p50'])}` faster `p50` · `{_sp(gs_pc['aggregate_ttfi_s']['p99'], gs_ba['aggregate_ttfi_s']['p99'])}` lower `p99`** |

### 4C. `C3-3c` — 5-Cluster `8,000`-Sandbox Single-Wave Ceiling (`500` Unique SWE-bench Images Across `5` Clusters)

| Metric (`c = 8,000`) | `per_claim` | **`batch64`** | Gain |
|---|---:|---:|---:|
| **Completed Sandboxes / Errors** | `{c8_pc['ok_all']:,} / 0` (`0` probe_err) | **`{c8_ba['ok_all']:,} / 0` (`0` probe_err)** | `100%` success at `c = 8,000` |
| **Acquire Wall (`claims/s`)** | `{c8_pc['acquire_wall_med_s']:.2f}s` (`{c8_pc['claims_per_s_med']:.1f}/s`) | **`{c8_ba['acquire_wall_med_s']:.2f}s` (`{c8_ba['claims_per_s_med']:.1f}/s`)** | **`{_sp(c8_pc['acquire_wall_med_s'], c8_ba['acquire_wall_med_s'])}` faster acquire** |
| **Full Burst + `8,000` Exec Wall** | `{c8_pc['burst_and_exec_wall_med_s']:.2f}s` | **`{c8_ba['burst_and_exec_wall_med_s']:.2f}s`** | **`{_sp(c8_pc['burst_and_exec_wall_med_s'], c8_ba['burst_and_exec_wall_med_s'])}` faster** |
| **Wire Claim (`First` / `P50` / `P99` / `Max`)** | `{c8_pc['wire_first_med_s']:.2f}s / {c8_pc['wire_p50_med_s']:.2f}s / {c8_pc['wire_p99_med_s']:.2f}s / {c8_pc['wire_max_med_s']:.2f}s` | **`{c8_ba['wire_first_med_s']:.2f}s / {c8_ba['wire_p50_med_s']:.2f}s / {c8_ba['wire_p99_med_s']:.2f}s / {c8_ba['wire_max_med_s']:.2f}s`** | **`{_sp(c8_pc['wire_p99_med_s'], c8_ba['wire_p99_med_s'])}` lower wire `p99`** |
| **True `TTFI` (`First` / `P50` / `P99` / `Max`)** | `{c8_pc['first_med_s']:.2f}s / {c8_pc['p50_med_s']:.2f}s / {c8_pc['p99_med_s']:.2f}s / {c8_pc['max_med_s']:.2f}s` | **`{c8_ba['first_med_s']:.2f}s / {c8_ba['p50_med_s']:.2f}s / {c8_ba['p99_med_s']:.2f}s / {c8_ba['max_med_s']:.2f}s`** | **`{_sp(c8_pc['p50_med_s'], c8_ba['p50_med_s'])}` faster `p50` · `{_sp(c8_pc['p99_med_s'], c8_ba['p99_med_s'])}` lower `p99`** |
| **Release API Return (`release_wall_s`)** | **`{c8_pc['release_wall_med_s']:.2f}s` (`{c8_pc['release_per_s_med']:.1f}/s`)** | `{c8_ba['release_wall_med_s']:.2f}s` (`{c8_ba['release_per_s_med']:.1f}/s`) | `per_claim` `1.15×` faster on API return |
| **Held Client Sockets / Driver RSS** | `{int(c8_pc['held_open_sockets_med']):,}` sockets / `{c8_pc['held_rss_mb_med']:.1f} MB` | **`{int(c8_ba['held_open_sockets_med']):,}` sockets / `{c8_ba['held_rss_mb_med']:.1f} MB`** | **`{_sp(c8_pc['held_open_sockets_med'], c8_ba['held_open_sockets_med'])}` fewer sockets · `-27.8%` RSS** |

### 4D. `C3-3d` — 5-Cluster Pure Python SDK `5,000`-Sandbox Claim & Release (`release_api_s` vs `claims_gone_s` vs `pods_gone_s`)

*(Standing `1,000` warm gVisor pods/cluster × `5` clusters = `5,000` warm pods ready in `{c3_3d['setup_s']:.2f}s`; `2` repeats at `n ∈ {{1000, 2500, 5000}}` for `batch` (`6` runs), `1` repeat at `n ∈ {{1000, 2500, 5000}}` for concurrent `terminate()` (`3` runs), and `1` repeat at `n = 5000` for `delete_all()` (`1` run) = `10` records total. `release_api_s` measures when the SDK release call returns alongside how many claimed pods are still alive at that instant; `claims_gone_s` measures when `SandboxClaim` count hits `0`; `pods_gone_s` measures when all claimed `Pod` objects are completely deleted across all `5` clusters.)*

| Burst Size (`n`) | SDK Arm | Claims `ok` | Acquire Median (`Rate`) | Release API Return (`release_api_s`, Pods Alive) | `SandboxClaim`s Gone (`claims_gone_s`) | **Claimed Pods Gone (`pods_gone_s`)** | Pool Sockets (`Before → Held`) |
|---|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows_3d)}

---

## 5. Complete Accounting of Runs & Metrics Where `batch` Trails `per_claim`

Every case in Campaign 3 where `per_claim` recorded a lower latency, faster wall/release time, or lower resource count than `batch` is listed below without omission:

{chr(10).join(loss_bullets)}

---

## 6. Raw JSON Inventory (`campaign-v3/raw/` — `21` Artifacts)

- [`raw/c3_1a_cold_sbd_symmetric_per_claim.json`](raw/c3_1a_cold_sbd_symmetric_per_claim.json) & [`raw/c3_1a_cold_sbd_symmetric_batch64.json`](raw/c3_1a_cold_sbd_symmetric_batch64.json) *(Symmetric First-Boot Cold SBD, `#39–#40`)*
- [`raw/c3_1a_anchor_exact_c2_per_claim.json`](raw/c3_1a_anchor_exact_c2_per_claim.json) & [`raw/c3_1a_anchor_exact_c2_batch64.json`](raw/c3_1a_anchor_exact_c2_batch64.json) *(Symmetric Hydrated SBD, `#41–#42`)*
- [`raw/c3_1a_anchor_t1_swebench3000_per_claim.json`](raw/c3_1a_anchor_t1_swebench3000_per_claim.json) & [`raw/c3_1a_anchor_t1_swebench3000_batch64.json`](raw/c3_1a_anchor_t1_swebench3000_batch64.json) *(Sequential Hydration Diagnostic)*
- [`raw/c3_1b_cold_swebench500_per_claim.json`](raw/c3_1b_cold_swebench500_per_claim.json) & [`raw/c3_1b_cold_swebench500_batch64.json`](raw/c3_1b_cold_swebench500_batch64.json)
- [`raw/c3_1c_warm_ttfi_1x1000_per_claim.json`](raw/c3_1c_warm_ttfi_1x1000_per_claim.json) & [`raw/c3_1c_warm_ttfi_1x1000_batch64.json`](raw/c3_1c_warm_ttfi_1x1000_batch64.json)
- [`raw/c3_1c_warm_ttfi_200x5_per_claim.json`](raw/c3_1c_warm_ttfi_200x5_per_claim.json) & [`raw/c3_1c_warm_ttfi_200x5_batch20.json`](raw/c3_1c_warm_ttfi_200x5_batch20.json)
- [`raw/c3_2_density_per_claim.json`](raw/c3_2_density_per_claim.json) & [`raw/c3_2_density_batch64.json`](raw/c3_2_density_batch64.json)
- [`raw/c3_3a_mc5_warm4000_per_claim.json`](raw/c3_3a_mc5_warm4000_per_claim.json) & [`raw/c3_3a_mc5_warm4000_batch64.json`](raw/c3_3a_mc5_warm4000_batch64.json)
- [`raw/c3_3b_mc5_golden_small_per_claim.json`](raw/c3_3b_mc5_golden_small_per_claim.json) & [`raw/c3_3b_mc5_golden_small_batch20.json`](raw/c3_3b_mc5_golden_small_batch20.json)
- [`raw/c3_3c_mc5_cuj1_8000_per_claim.json`](raw/c3_3c_mc5_cuj1_8000_per_claim.json) & [`raw/c3_3c_mc5_cuj1_8000_batch64.json`](raw/c3_3c_mc5_cuj1_8000_batch64.json)
- [`raw/c3_3d_mc5_sdk_5000_claim_and_cleanup.json`](raw/c3_3d_mc5_sdk_5000_claim_and_cleanup.json)
"""


def render_11_05_results_md(s: dict) -> str:
    c2 = s["campaign_2"]
    c3 = s["campaign_3"]
    tot3 = s["campaign_3_totals"]
    c3_3d = c3["c3_3d_mc5_sdk_5000"]
    assert c3_3d is not None, "C3-3d must be present!"

    c2_ops = 148544
    grand_ops = c2_ops + tot3["total_sandbox_operations"]

    # C2 shortcuts
    n_pc = c2["phase1_t0_3000"]["naive"]["per_claim"]["variants"][0]
    n_ba = c2["phase1_t0_3000"]["naive"]["batch64"]["variants"][0]
    sl_pc = c2["phase1_t0_3000"]["sliding"]["per_claim"]["variants"][0]
    sl_ba = c2["phase1_t0_3000"]["sliding"]["batch64"]["variants"][0]
    pi_pc = c2["phase1_t0_3000"]["pipelined"]["per_claim"]["variants"][0]
    pi_ba = c2["phase1_t0_3000"]["pipelined"]["batch64"]["variants"][0]
    rc_pc = c2["phase1_t0_3000"]["recycle"]["per_claim"]["variants"][0]
    rc_ba = c2["phase1_t0_3000"]["recycle"]["batch64"]["variants"][0]

    r2e_pc = c2["phase2_rl_loops"]["r2e_multistep"]["per_claim"]["variants"][0]
    r2e_ba = c2["phase2_rl_loops"]["r2e_multistep"]["batch64"]["variants"][0]
    grpo_pc = c2["phase2_rl_loops"]["tunix_grpo"]["per_claim"]["variants"][0]
    grpo_ba = c2["phase2_rl_loops"]["tunix_grpo"]["batch64"]["variants"][0]

    soak_pc = c2["phase2_soak_10k"]["per_claim"]
    soak_ba = c2["phase2_soak_10k"]["batch64"]

    t1n_pc = c2["phase3_t1_sbd_3000"]["naive"]["per_claim"]["variants"][0]
    t1n_ba = c2["phase3_t1_sbd_3000"]["naive"]["batch64"]["variants"][0]
    t1p_pc = c2["phase3_t1_sbd_3000"]["pipelined"]["per_claim"]["variants"][0]
    t1p_ba = c2["phase3_t1_sbd_3000"]["pipelined"]["batch64"]["variants"][0]
    t1s_pc = c2["phase3_t1_sbd_3000"]["sleep30"]["per_claim"]["variants"][0]
    t1s_ba = c2["phase3_t1_sbd_3000"]["sleep30"]["batch64"]["variants"][0]
    s3a_pipe_pc = next(v for v in c2["phase2_configH_sweeps"]["swe500x1"]["per_claim"]["variants"] if v["label"] == "pipelined")
    s3a_pipe_ba = next(v for v in c2["phase2_configH_sweeps"]["swe500x1"]["batch64"]["variants"] if v["label"] == "pipelined")

    # C3 shortcuts
    cs_pc = c3["c3_1a_cold_sbd_symmetric"]["per_claim"]["variants"][0]
    cs_ba = c3["c3_1a_cold_sbd_symmetric"]["batch64"]["variants"][0]
    u_pc = c3["c3_1a_anchor_unhydrated_sbd"]["per_claim"]["variants"][0]
    u_ba = c3["c3_1a_anchor_unhydrated_sbd"]["batch64"]["variants"][0]
    h_pc = c3["c3_1a_anchor_hydrated_exact_c2"]["per_claim"]["variants"][0]
    h_ba = c3["c3_1a_anchor_hydrated_exact_c2"]["batch64"]["variants"][0]
    parity_pct = abs(h_ba["wall_s"] - t1n_ba["wall_s"]) / t1n_ba["wall_s"] * 100.0
    b_pc = c3["c3_1b_cold_gcfs_500"]["per_claim"]["variants"][0]
    b_ba = c3["c3_1b_cold_gcfs_500"]["batch64"]["variants"][0]
    b2a_pc = c2["phase2_burst_curves"]["1x1000_configH"]["per_claim"]
    b2a_ba = c2["phase2_burst_curves"]["1x1000_configH"]["batch64"]
    sdk1k = {r["mode"]: r for r in c2["phase2_sdk_1000_cleanup"]["summary"]}

    w1_pc = c3["c3_1c_warm_ttfi_1x1000"]["per_claim"]
    w1_ba = c3["c3_1c_warm_ttfi_1x1000"]["batch64"]
    w2_pc = c3["c3_1c_warm_ttfi_200x5"]["per_claim"]
    w2_ba = c3["c3_1c_warm_ttfi_200x5"]["batch20"]

    d_pc = c3["c3_2_density"]["per_claim"]
    d_ba = c3["c3_2_density"]["batch64"]
    d24_pc = _pt_by_c(d_pc, 2400)
    d24_ba = _pt_by_c(d_ba, 2400)

    m4_pc = c3["c3_3a_mc5_warm4000"]["per_claim"]
    m4_ba = c3["c3_3a_mc5_warm4000"]["batch64"]
    m4_4k_pc = _pt_by_c(m4_pc, 4000)
    m4_4k_ba = _pt_by_c(m4_ba, 4000)

    gs_pc = c3["c3_3b_mc5_golden_small"]["per_claim"]
    gs_ba = c3["c3_3b_mc5_golden_small"]["batch20"]

    c8_pc = c3["c3_3c_mc5_cuj1_8000"]["per_claim"]["points"][0]
    c8_ba = c3["c3_3c_mc5_cuj1_8000"]["batch64"]["points"][0]

    b1000 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 1000)
    p1000 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 1000)
    b2500 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 2500)
    p2500 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 2500)
    b5000 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 5000)
    p5000 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 5000)
    d5000 = _c3_3d_row(c3_3d, "per_claim_sdk_delete_all", 5000)

    w1_rows = []
    for c_val in (100, 200, 500, 1000):
        pp = _pt_by_c(w1_pc, c_val)
        pb = _pt_by_c(w1_ba, c_val)
        w1_rows.append(
            f"| **`c = {c_val:,}`** | `{pp['first_med_s']:.2f}s / {pp['p50_med_s']:.2f}s / {pp['p99_med_s']:.2f}s / {pp['max_med_s']:.2f}s` (`{pp['wire_p50_med_s']:.2f}s` wire) | **`{pb['first_med_s']:.2f}s / {pb['p50_med_s']:.2f}s / {pb['p99_med_s']:.2f}s / {pb['max_med_s']:.2f}s`** (`{pb['wire_p50_med_s']:.2f}s` wire) | **`{_sp(pp['p50_med_s'], pb['p50_med_s'])}`** `TTFI P50` · **`{_sp(pp['p99_med_s'], pb['p99_med_s'])}`** `TTFI P99` |"
        )

    w2_rows = []
    for c_val in (100, 200, 500, 1000):
        pp = _pt_by_c(w2_pc, c_val)
        pb = _pt_by_c(w2_ba, c_val)
        w2_rows.append(
            f"| **`c = {c_val:,}`** | `{pp['first_med_s']:.2f}s / {pp['p50_med_s']:.2f}s / {pp['p99_med_s']:.2f}s / {pp['max_med_s']:.2f}s` · `{int(pp['held_open_sockets_med'])}` socks | **`{pb['first_med_s']:.2f}s / {pb['p50_med_s']:.2f}s / {pb['p99_med_s']:.2f}s / {pb['max_med_s']:.2f}s`** · **`{int(pb['held_open_sockets_med'])}` socks** | **`{_sp(pp['p50_med_s'], pb['p50_med_s'])}`** `TTFI P50` · **`{_sp(pp['held_open_sockets_med'], pb['held_open_sockets_med'])}`** fewer sockets |"
        )

    dens_rows = []
    for c_val in (640, 1600, 2400):
        pp = _pt_by_c(d_pc, c_val)
        pb = _pt_by_c(d_ba, c_val)
        dens_rows.append(
            f"| **`{c_val:,}` (`{pb['active_sandboxes_per_vcpu']}/vCPU`)** | `{pp['acquire_wall_s']:.2f}s` (`{pp['claims_per_s']:.1f}/s`) · `{pp['ttfi_s']['p50']:.2f}s / {pp['ttfi_s']['p99']:.2f}s` · `{pp['open_sockets_held']}` socks | **`{pb['acquire_wall_s']:.2f}s` (`{pb['claims_per_s']:.1f}/s`)** · **`{pb['ttfi_s']['p50']:.2f}s / {pb['ttfi_s']['p99']:.2f}s`** · **`{pb['open_sockets_held']}` socks** | **`{_sp(pp['acquire_wall_s'], pb['acquire_wall_s'])}`** acquire · **`{_sp(pp['ttfi_s']['p50'], pb['ttfi_s']['p50'])}`** `TTFI P50` · `0` OOM/evict |"
        )

    mc4_rows = []
    for c_val in (1000, 2000, 4000):
        pp = _pt_by_c(m4_pc, c_val)
        pb = _pt_by_c(m4_ba, c_val)
        mc4_rows.append(
            f"| **`c = {c_val:,}`** | `{pp['acquire_wall_med_s']:.2f}s` (`{pp['claims_per_s_med']:.1f}/s`) · `{pp['p50_med_s']:.2f}s / {pp['p99_med_s']:.2f}s / {pp['max_med_s']:.2f}s` · `{int(pp['held_open_sockets_med'])}` socks | **`{pb['acquire_wall_med_s']:.2f}s` (`{pb['claims_per_s_med']:.1f}/s`)** · **`{pb['p50_med_s']:.2f}s / {pb['p99_med_s']:.2f}s / {pb['max_med_s']:.2f}s`** · **`{int(pb['held_open_sockets_med'])}` socks** | **`{_sp(pp['acquire_wall_med_s'], pb['acquire_wall_med_s'])}`** acquire · **`{_sp(pp['p50_med_s'], pb['p50_med_s'])}`** `TTFI P50` |"
        )

    sdk_5c_rows = []
    for b_r, p_r in ((b1000, p1000), (b2500, p2500), (b5000, p5000)):
        n_v = b_r["n"]
        p_terr = f" *(+{p_r['term_err_all']} term_err)*" if p_r.get("term_err_all") else ""
        sdk_5c_rows.append(
            f"| `{n_v:,}` (`{n_v // 5}`/cluster) | `per_claim` concurrent `terminate()` (`1` rep) | `{p_r['ok_all']}/{n_v * p_r['repeats']}`{p_terr} | `{p_r['acquire_med_s']:.2f}s` (`{p_r['creates_per_s_med']:.1f}/s`) | `{p_r['release_api_med_s']:.2f}s` (`{p_r['release_api_per_s_med']:,.1f}/s`, `{int(p_r['pods_alive_at_api_return_med'])}` alive) | `{p_r['claims_gone_med_s']:.2f}s` | `{p_r['pods_gone_med_s']:.2f}s` (`{p_r['pods_gone_per_s_med']:.1f} pods/s`) | `{int(p_r['socks_before_med'])} → {int(p_r['held_open_sockets_med'])}` |\n"
            f"| **`{n_v:,}` (`{n_v // 5}`/cluster)** | **`SandboxBatch.release()` (`DELETE_COLLECTION`, `2` reps)** | **`{b_r['ok_all']}/{n_v * b_r['repeats']}`** | **`{b_r['acquire_med_s']:.2f}s` (`{b_r['creates_per_s_med']:.1f}/s`)** | **`{b_r['release_api_med_s']:.2f}s` (`{b_r['release_api_per_s_med']:,.1f}/s`, `{int(b_r['pods_alive_at_api_return_med'])}` alive)** | **`{b_r['claims_gone_med_s']:.2f}s`** | **`{b_r['pods_gone_med_s']:.2f}s` (`{b_r['pods_gone_per_s_med']:.1f} pods/s`)** | **`{int(b_r['socks_before_med'])} → {int(b_r['held_open_sockets_med'])}`** |"
        )
    sdk_5c_rows.append(
        f"| `{d5000['n']:,}` (`1,000`/cluster) | `AsyncSandboxClient.delete_all()` (`1` rep) | `{d5000['ok_all']}/{d5000['n']}` | `{d5000['acquire_med_s']:.2f}s` (`{d5000['creates_per_s_med']:.1f}/s`) | `{d5000['release_api_med_s']:.2f}s` (`{d5000['release_api_per_s_med']:,.1f}/s`, `{int(d5000['pods_alive_at_api_return_med'])}` alive) | `{d5000['claims_gone_med_s']:.2f}s` | `{d5000['pods_gone_med_s']:.2f}s` (`{d5000['pods_gone_per_s_med']:.1f} pods/s`) | `{int(d5000['socks_before_med'])} → {int(d5000['held_open_sockets_med'])}` |"
    )

    return f"""# Agent Sandbox for RL & Evals — `SandboxBatch` Performance, True TTFI & 5-Cluster Scale Report (`2026-11-05` / `2026-11-07`)

> **Author:** Brian Nguyen · **Environment:** Regional GKE `v1.37.1-gke.1697000` (`us-central1`; single-cluster `48` control-plane vCPUs + `320` worker vCPUs, and 5-cluster fleet `240` control-plane vCPUs + `1,600` worker vCPUs)
>
> **Scope:** Controlled, matched-cluster head-to-head evaluation comparing legacy per-sandbox claiming (`claim_mode="per_claim"`) against **`SandboxBatch` (`claim_mode="batch"`)** across **`{grand_ops:,}` real sandbox claims and multi-step RL trajectories** (`57` total benchmark runs: `36` in Campaign 2 + `21` in Campaign 3 across **`1,000` distinct multi-GB repository images** — `500` `SWE-bench Verified` + `500` `R2E-Gym`).

---

## Executive Summary — Results Mapped to Live Product Metrics

| Product Metric Dimension | Measured Result with `SandboxBatch` (`claim_mode="batch"`) | Matched-Cluster Gain vs `HEAD per_claim` (& Baselines) |
|---|---|---|
| **1. Scale & Control-Plane Reliability**<br>*(Zero `etcd` / API Saturation & Multi-Cluster Ceiling)* | • **`0` claim errors (`0.000%` claim failure rate) across every `SandboxBatch` run in Campaigns 2 & 3**, **`0` controller restarts**, **`0` leader-election drops**, and **`0` leaked objects** *(`1` transient exec `probe_err` on `batch` vs `4` `probe_err`s + `{tot3['total_claim_errors']}` multi-cluster Watch-churn `ConnectionTimeoutError`s + `1` `terminate()` error on `per_claim` across `{grand_ops:,}` total ops)*<br>• **5-cluster simultaneous wave:** **`8,000 / 8,000` concurrent gVisor SWE-bench sandboxes** acquired in **`{c8_ba['acquire_wall_med_s']:.2f}s` (`{c8_ba['claims_per_s_med']:.1f} claims/s`)** | • **`{_sp(c8_pc['acquire_wall_med_s'], c8_ba['acquire_wall_med_s'])}` faster `8,000`-sandbox acquire** (`{c8_pc['acquire_wall_med_s']:.2f}s → {c8_ba['acquire_wall_med_s']:.2f}s`) and **`{_sp(c8_pc['held_open_sockets_med'], c8_ba['held_open_sockets_med'])}` fewer sockets** (`{int(c8_pc['held_open_sockets_med']):,} → {int(c8_ba['held_open_sockets_med']):,}`)<br>• `N` watches → **`1` shared watch/cluster**; `N` deletes → **`1` `DELETE_COLLECTION`/cluster** |
| **2. True Warm & Cold Startup Latency (`TTFI`)**<br>*(Claim Issued → First Container Instruction Returned)* | • **Single `1,000`-pod warm pool (`C3-1c` medians):**<br>  - `c=100`: `First={_pt_by_c(w1_ba, 100)['first_med_s']:.2f}s` · **`P50={_pt_by_c(w1_ba, 100)['p50_med_s']:.2f}s`** · `P99={_pt_by_c(w1_ba, 100)['p99_med_s']:.2f}s`<br>  - `c=500`: `First={_pt_by_c(w1_ba, 500)['first_med_s']:.2f}s` · **`P50={_pt_by_c(w1_ba, 500)['p50_med_s']:.2f}s`** · `P99={_pt_by_c(w1_ba, 500)['p99_med_s']:.2f}s`<br>  - `c=1,000`: `First={_pt_by_c(w1_ba, 1000)['first_med_s']:.2f}s` · **`P50={_pt_by_c(w1_ba, 1000)['p50_med_s']:.2f}s`** · `P99={_pt_by_c(w1_ba, 1000)['p99_med_s']:.2f}s`<br>• **Wire-only `1,000`-pod burst (`Bench 2A` 3-repeat `avg_med_s`):** **`{_pt_by_c(b2a_ba, 100)['avg_med_s']:.2f}s / {_pt_by_c(b2a_ba, 200)['avg_med_s']:.2f}s / {_pt_by_c(b2a_ba, 500)['avg_med_s']:.2f}s / {_pt_by_c(b2a_ba, 1000)['avg_med_s']:.2f}s`** at `c=100..1,000`<br>• **Cold `0%`-cached GCFS (`500` unique images):** **`{b_ba['wall_s']:.2f}s` wall**, `TTFI P50={b_ba['paired_ttfi_s']['p50']:.2f}s` (`P99={b_ba['paired_ttfi_s']['p99']:.2f}s`) | • **`1.83×–2.31×` faster true warm `TTFI P50`** at `c=100..1,000` (`{_pt_by_c(w1_pc, 1000)['p50_med_s']:.2f}s → {_pt_by_c(w1_ba, 1000)['p50_med_s']:.2f}s` at `c=1,000`)<br>• **`{_sp(_pt_by_c(b2a_pc, 1000)['avg_med_s'], _pt_by_c(b2a_ba, 1000)['avg_med_s'])}` faster wire claim mean** at `c=1,000` vs matched `per_claim` (`{_pt_by_c(b2a_pc, 100)['avg_med_s']:.2f}s / {_pt_by_c(b2a_pc, 200)['avg_med_s']:.2f}s / {_pt_by_c(b2a_pc, 500)['avg_med_s']:.2f}s / {_pt_by_c(b2a_pc, 1000)['avg_med_s']:.2f}s`)<br>• **`{_sp(b_pc['wall_s'], b_ba['wall_s'])}` faster cold `0%`-cached `500`-image pull** (`{b_pc['wall_s']:.2f}s → {b_ba['wall_s']:.2f}s`) |
| **3. RL Step Barrier & Tail Latency (`P99` / `P100`)**<br>*(GPU-Blocking Straggler Across Single & Multi-Job RL)* | • **`R2E-Gym` 8-step RL (`8,000` rollouts, `c=500`):** Steady-state **step barrier (`P100`) = `28.48s`** (`390.77s` total wall)<br>• **5-cluster `8 × 512 = 4,096` concurrent RL jobs (`C3-3b`):** Per-job **`P100` step barrier = `{gs_ba['job_step_barrier_s']['max']:.2f}s`** (`all_jobs_wall_s = {gs_ba['all_jobs_wall_s']:.2f}s`, `TTFI P99 = {gs_ba['aggregate_ttfi_s']['p99']:.2f}s`)<br>• **`T0` SWE-bench (`3,000` tasks, stock APF):** Claim **`P99 = 19.30s`** (`P100 = 32.30s`) | • **`4.86×` faster steady-state R2E-Gym RL step barrier** (`138.32s → 28.48s`; **`3.82×` faster** 8-step wall `1,494.54s → 390.77s`)<br>• **`{_sp(gs_pc['job_step_barrier_s']['max'], gs_ba['job_step_barrier_s']['max'])}` lower 8-job `4,096`-sandbox barrier** (`{gs_pc['job_step_barrier_s']['max']:.2f}s → {gs_ba['job_step_barrier_s']['max']:.2f}s`)<br>• **`7.24×` lower `T0` claim `P99`** (`139.66s → 19.30s`) |
| **4. Create & Bulk Teardown Throughput**<br>*(API Return `release_api_s` vs Actual `pods_gone_s`)* | • **5-cluster warm create rate:** **`{c8_ba['claims_per_s_med']:.1f} claims/s`** at `c=8,000` (`{b5000['creates_per_s_med']:.1f} claims/s` pure SDK at `n=5,000`)<br>• **1-cluster `1,000`-claim release (`p2_sdk_1000_cleanup`):** **`{min(sdk1k['batch_release_deletecollection']['release_api_runs_s']):.2f}s` best / `{sdk1k['batch_release_deletecollection']['release_api_med_s']:.2f}s` median** API return<br>• **5-cluster `5,000`-claim release (`C3-3d`):** **`{b5000['release_api_med_s']:.2f}s` API return (`{b5000['release_api_per_s_med']:,.1f}/s`)** · **`{b5000['pods_gone_med_s']:.2f}s` until all `5,000` pods deleted (`{b5000['pods_gone_per_s_med']:.1f} pods/s`)** | • **`{_sp(sdk1k['per_claim_sdk_delete_all']['release_api_med_s'], min(sdk1k['batch_release_deletecollection']['release_api_runs_s']))}` best (`{_sp(sdk1k['per_claim_sdk_delete_all']['release_api_med_s'], sdk1k['batch_release_deletecollection']['release_api_med_s'])}` median) faster 1-cluster release API return** vs `delete_all()` (`{sdk1k['per_claim_sdk_delete_all']['release_api_med_s']:.2f}s`), and **`{_sp(d5000['release_api_med_s'], b5000['release_api_med_s'])}` faster API return (`{_sp(d5000['pods_gone_med_s'], b5000['pods_gone_med_s'])}` faster `pods_gone_s`)** vs `delete_all()` at `n=5,000`<br>• Fresh `batch` (`87.40s`) beats in-place gVisor `git restore` (`429.24s`) by **`4.91×`** |
| **5. Compute Packing Density & Client Footprint**<br>*(Validated Active Sandboxes / vCPU)* | • **Validated active packing (`C3-2`):** **`2.0`, `5.0`, and `7.5` active gVisor sandboxes / vCPU** (**`2,400` active SWE-bench pods** across `10 × n2-standard-32` = `240` pods/node, `0` OOMs/evictions, `TTFI P50 = {d24_ba['ttfi_s']['p50']:.2f}s`)<br>• **Client footprint:** flat **`44` sockets** on `200×5` warm fleet; **`320.9 MB` RSS** over `10,000`-claim soak | • **`{_sp(d24_pc['ttfi_s']['p50'], d24_ba['ttfi_s']['p50'])}` faster `TTFI P50` at `7.5` sandboxes/vCPU** (`{d24_pc['ttfi_s']['p50']:.2f}s → {d24_ba['ttfi_s']['p50']:.2f}s`)<br>• **Up to `11.1×` fewer open sockets** (`488 → 44`) and **`-37.3%` RSS / `-38.4%` threads** over `50 × 200` soak waves |
| **6. Image Cardinality & Cache Tiers**<br>*(`1,000` Unique Images · `T0` GCFS vs `T1` `600 GB` SBD)* | • **`1,000` unique `1.2–2.6 GB` images** (`500` SWE-bench + `500` R2E-Gym)<br>• **`T1` `600 GB` Preloaded SBD (`CONTAINER_IMAGE_CACHE`):** `0` registry pulls, **`0.28–0.38s` gVisor exec `p50`**, **`{t1p_ba['wall_s']:.2f}s`** `pipelined` infra floor (`#34`), **`{t1s_ba['wall_s']:.2f}s` wall** on `3,000` real-work (`git + 30s`) tasks (`#36`, `22.9 ms/task` amortized infra overhead above `180s` sleep floor) | • **`{_sp(t1p_pc['wall_s'], t1p_ba['wall_s'])}` faster hydrated `pipelined` wall** (`{t1p_pc['wall_s']:.2f}s → {t1p_ba['wall_s']:.2f}s`, **`4.50×`–`5.06×` fewer sockets**); on symmetric `wp=1` `naive` + 3-lane APF (`#39/#40` cold SBD `{cs_pc['wall_s']:.2f}s` vs `{cs_ba['wall_s']:.2f}s`; `#41/#42` hydrated SBD `{h_pc['wall_s']:.2f}s` vs `{h_ba['wall_s']:.2f}s`), `per_claim` streams deletes `1.09×–1.17×` faster while `batch` cuts sockets **`4.51×–4.68×`** *(C2 `#31/#32` `281.65s → 139.10s` was asymmetric due to `#31`'s first-boot SBD hydration; see §5A)* |

---

## 1. Control-Plane Protection — Why `SandboxBatch` Prevents `etcd` & `kube-apiserver` Failures

Synchronized RL step barriers create bursty control-plane storms: launching or tearing down `1,000–8,000` sandboxes at once via individual `POST`s, `N` concurrent watch streams, and `N` individual `DELETE`s saturates `kube-apiserver` watch encoders, `etcd` write transactions, and API Priority & Fairness (`APF`) concurrency seats—triggering HTTP `429` cascades, readiness timeouts, and controller leader-election losses.

Across all **`{grand_ops:,}` claims and rollouts** in Campaigns 2 and 3, **`SandboxBatch` recorded `0` claim errors across every single run (`0` controller restarts, `0` leader-election losses, and `0` orphaned objects)**, whereas `per_claim` suffered `{tot3['total_claim_errors']}` external GKE VIP `ConnectionTimeoutError`s (plus `1` `terminate()` error) during `C3-3d`'s `2,500–5,000` multi-cluster SDK Watch churn:

1. **`N` Watch Streams → `1` Shared Label-Filtered Watch per Cluster:** `SandboxBatch` multiplexes an entire batch over `1` watch (`agents.x-k8s.io/batch-id`), cutting client sockets up to **`11.1×` (`488 → 44`)** on single clusters and **`{_sp(c8_pc['held_open_sockets_med'], c8_ba['held_open_sockets_med'])}` (`{int(c8_pc['held_open_sockets_med']):,} → {int(c8_ba['held_open_sockets_med']):,}`)** on `8,000`-sandbox 5-cluster bursts, while keeping its `{int(b5000['socks_before_med'])} → {int(b5000['held_open_sockets_med'])}` warm keep-alive pool intact in `C3-3d` (`per_claim`'s `Watch.stop()` closed pooled sockets from `{int(p5000['socks_before_med'])} → {int(p5000['held_open_sockets_med'])}`).
2. **`N` Individual `DELETE`s → `1` `DELETE_COLLECTION` + Fenced `Lease` GC (`500×` Fewer Delete RPCs):** At `N = 5,000` across `5` clusters, `batch.release()` issues `5` `DELETE_COLLECTION` calls + `5` `Lease` deletes (`10` RPCs vs `5,000` individual `DELETE`s), backed by TLA+-verified `resourceVersion`-fenced `Lease` crash recovery.
3. **3-Lane GKE APF Insulation:** Within GKE's `30`-share custom `PriorityLevelConfiguration` budget, controller reconciles run in `agent-sandbox-critical` (`20` shares, `lendablePercent=0`), bulk client bursts run in `agent-sandbox-bulk` (`10` shares), and controller leader-election `Lease` renewals stay on `global-default`.
4. **Control-Plane & Multi-Cluster Cloud NAT Sizing:** Every regional GKE `1.37` cluster was verified at `go_sched_gomaxprocs_threads = 16` (`48` control-plane vCPUs/cluster), and the 5-cluster private-node VPC used **`8` static external Cloud NAT IPs** (`maxPortsPerVm=32,768`) + `900s` `aiohttp` keepalive pooling so multi-cluster bursts never hit single-IP L4 load-balancer SYN rate limits.

---

## 2. True End-to-End Startup Latency (`TTFI`: Warm Standing Pools & Cold `0%`-Cached GCFS)

### 2A. Single-Cluster `1,000`-Pod Standing Warm Pools — True `TTFI` (`C3-1c`, `3`-Repeat Medians, `5,400` Claims / Arm)

Unlike wire-only claim latency (`Bench 2A` 3-repeat `avg_med_s`: `{_pt_by_c(b2a_ba, 100)['avg_med_s']:.2f}s / {_pt_by_c(b2a_ba, 200)['avg_med_s']:.2f}s / {_pt_by_c(b2a_ba, 500)['avg_med_s']:.2f}s / {_pt_by_c(b2a_ba, 1000)['avg_med_s']:.2f}s` on `batch64` vs `{_pt_by_c(b2a_pc, 100)['avg_med_s']:.2f}s / {_pt_by_c(b2a_pc, 200)['avg_med_s']:.2f}s / {_pt_by_c(b2a_pc, 500)['avg_med_s']:.2f}s / {_pt_by_c(b2a_pc, 1000)['avg_med_s']:.2f}s` on `per_claim` at `c = 100 / 200 / 500 / 1,000`), `C3-1c` measures **true end-to-end `TTFI`** from client claim call until `python3 -V` executes inside the gVisor container and returns stdout (`3` repeats per concurrency point):

#### Single `1,000`-Replica Warm Pool (`1 × 1,000`)

| Concurrency | `HEAD per_claim` (`First / P50 / P99 / P100`) | **`SandboxBatch` `mif=64` (`First / P50 / P99 / P100`)** | Matched Speedup |
|---:|---|---|---|
{chr(10).join(w1_rows)}

#### Multi-Pool `200 × 5 = 1,000` Standing Warm Fleet (`per_claim` vs SDK Default `batch20`)

| Concurrency | `HEAD per_claim` (`First / P50 / P99 / P100` · Sockets) | **`SandboxBatch` `mif=20` (`First / P50 / P99 / P100` · Sockets)** | Matched Speedup |
|---:|---|---|---|
{chr(10).join(w2_rows)}

*(Note: At `c = 1,000` on `200×5`, `per_claim` experienced `4` transient container exec probe errors out of `3,000` claims, whereas `batch20` completed `3,000 / 3,000` with `0` claim and `0` probe errors.)*

### 2B. Truly Cold `0%`-Cached `T0` GCFS Image Streaming (`C3-1b`, `500` Unique SWE-bench Images × `1`, `c = 500`)

To measure genuine cold-node startup without cross-run cache contamination, the `10 × e2-standard-32` `gvisor-pool` (`200 GB pd-balanced`) was **deleted and recreated from scratch immediately before each arm**:

| Arm (`RESULTS.md` Row) | Total Wall (`tasks/s`) | `wait_pool_ready` `avg / max` | Paired `TTFI` (`First / P50 / P99 / P100`) | Open Sockets | Speedup |
|---|---:|---:|---|---:|---|
| `per_claim` (`#37`) | `{b_pc['wall_s']:.2f}s` (`{b_pc['tasks_per_s']:.2f}/s`) | `{b_pc['wpr_avg_s']:.2f}s / {b_pc['wpr_max_s']:.2f}s` | `{b_pc['paired_ttfi_s']['first']:.2f}s / {b_pc['paired_ttfi_s']['p50']:.2f}s / {b_pc['paired_ttfi_s']['p99']:.2f}s / {b_pc['paired_ttfi_s']['max']:.2f}s` | `{b_pc['open_sockets_after']}` | `1.00×` |
| **`batch64` (`#38`)** | **`{b_ba['wall_s']:.2f}s` (`{b_ba['tasks_per_s']:.2f}/s`)** | **`{b_ba['wpr_avg_s']:.2f}s / {b_ba['wpr_max_s']:.2f}s`** | **`{b_ba['paired_ttfi_s']['first']:.2f}s / {b_ba['paired_ttfi_s']['p50']:.2f}s / {b_ba['paired_ttfi_s']['p99']:.2f}s / {b_ba['paired_ttfi_s']['max']:.2f}s`** | `{b_ba['open_sockets_after']}` | **`{_sp(b_pc['wall_s'], b_ba['wall_s'])}` faster wall · `{_sp(b_pc['paired_ttfi_s']['p50'], b_ba['paired_ttfi_s']['p50'])}` faster `TTFI P50`** |

---

## 3. Multi-Cluster Horizontal Scale (`5` Regional GKE Clusters, `1,600` Worker vCPUs, `4,000–8,000` Concurrent Sandboxes)

### 3A. `C3-3a` — 5-Cluster `4,000`-Pod Warm Fleet Burst Curve (`2`-Repeat Medians = `14,000` Claims / Arm)

| Concurrency (`5` Clusters) | `HEAD per_claim` (`Acquire` · `TTFI P50 / P99 / P100` · Sockets) | **`SandboxBatch` `batch64` (`Acquire` · `TTFI P50 / P99 / P100` · Sockets)** | Gain |
|---:|---|---|---|
{chr(10).join(mc4_rows)}

*(At `c = 4,000`, both arms succeeded on `8,000 / 8,000` claims; `batch64` recorded `1` transient exec probe error out of `8,000` probes while cutting `TTFI P50` **`{_sp(m4_4k_pc['p50_med_s'], m4_4k_ba['p50_med_s'])}`** from `{m4_4k_pc['p50_med_s']:.2f}s → {m4_4k_ba['p50_med_s']:.2f}s` and held sockets **`{_sp(m4_4k_pc['held_open_sockets_med'], m4_4k_ba['held_open_sockets_med'])}`**.)*

### 3B. `C3-3b` & `C3-3c` — `4,096`-Sandbox Multi-Job RL Barrier (`8 × 512`) & `8,000`-Sandbox Single-Wave Ceiling

| Multi-Cluster Workload (`5` Clusters, `500` Unique Images) | `HEAD per_claim` | **`SandboxBatch`** | Pure `SandboxBatch` Improvement |
|---|---:|---:|---|
| **`C3-3b`: `8` Concurrent RL Jobs × `512` = `4,096` Sandboxes**<br>*(All-jobs wall · Per-job `P100` step barrier · Aggregate `TTFI P50 / P99`)* | `{gs_pc['all_jobs_wall_s']:.2f}s` (`{gs_pc['aggregate_goodput_tasks_per_s']:.1f}/s`)<br>Job `P100`: `{gs_pc['job_step_barrier_s']['max']:.2f}s` · `TTFI`: `{gs_pc['aggregate_ttfi_s']['p50']:.2f}s / {gs_pc['aggregate_ttfi_s']['p99']:.2f}s` | **`{gs_ba['all_jobs_wall_s']:.2f}s` (`{gs_ba['aggregate_goodput_tasks_per_s']:.1f}/s`)**<br>Job `P100`: **`{gs_ba['job_step_barrier_s']['max']:.2f}s`** · `TTFI`: **`{gs_ba['aggregate_ttfi_s']['p50']:.2f}s / {gs_ba['aggregate_ttfi_s']['p99']:.2f}s`** | **`{_sp(gs_pc['all_jobs_wall_s'], gs_ba['all_jobs_wall_s'])}` total wall** · **`{_sp(gs_pc['job_step_barrier_s']['max'], gs_ba['job_step_barrier_s']['max'])}` lower job `P100` barrier** · **`{_sp(gs_pc['aggregate_ttfi_s']['p50'], gs_ba['aggregate_ttfi_s']['p50'])}` faster `TTFI P50`** (`4,096/4,096` OK) |
| **`C3-3c`: `8,000`-Sandbox Single-Wave Burst**<br>*(Acquire wall · Full burst+exec wall · `TTFI P50 / P99` · Sockets)* | `{c8_pc['acquire_wall_med_s']:.2f}s` (`{c8_pc['claims_per_s_med']:.1f}/s`) · `{c8_pc['burst_and_exec_wall_med_s']:.2f}s` total<br>`TTFI`: `{c8_pc['p50_med_s']:.2f}s / {c8_pc['p99_med_s']:.2f}s` · `{int(c8_pc['held_open_sockets_med']):,}` socks | **`{c8_ba['acquire_wall_med_s']:.2f}s` (`{c8_ba['claims_per_s_med']:.1f}/s`)** · **`{c8_ba['burst_and_exec_wall_med_s']:.2f}s` total**<br>`TTFI`: **`{c8_ba['p50_med_s']:.2f}s / {c8_ba['p99_med_s']:.2f}s`** · **`{int(c8_ba['held_open_sockets_med']):,}` socks** | **`{_sp(c8_pc['acquire_wall_med_s'], c8_ba['acquire_wall_med_s'])}` faster acquire** · **`{_sp(c8_pc['p99_med_s'], c8_ba['p99_med_s'])}` lower `TTFI P99`** · **`{_sp(c8_pc['held_open_sockets_med'], c8_ba['held_open_sockets_med'])}` fewer sockets** (`8,000/8,000` OK) |

### 3C. `C3-3d` — 5-Cluster Pure Python SDK `5,000`-Sandbox Teardown: API Return (`release_api_s`) vs Actual Pod Deletion (`pods_gone_s`)

> **Important Measurement Distinction:** `release_api_s` measures when the client deletion RPC (`DELETE_COLLECTION`, concurrent `terminate()`, or `delete_all()`) returns from `kube-apiserver`. When `batch.release()` returned at **`{b5000['release_api_med_s']:.2f}s`**, **`{int(b5000['pods_alive_at_api_return_med']):,} / 5,000` (`{b5000['pods_alive_at_api_return_med'] / 50.0:.1f}%`) claimed gVisor pods were still alive** because Kubernetes garbage collection (`SandboxClaim → Sandbox → Pod`) and Kubelet container teardown run asynchronously. `C3-3d` therefore records `claims_gone_s` (`0` `SandboxClaim`s left) and `pods_gone_s` (`0` claimed `Pod` objects left across all `5` clusters, `terminationGracePeriodSeconds: 0`).

| Burst Size (`5` Clusters) | SDK Teardown Method | Claims `ok` | Acquire Median (`Rate`) | Release API Return (`release_api_s`, Pods Alive) | `SandboxClaim`s Gone (`claims_gone_s`) | **Actual Claimed Pods Gone (`pods_gone_s`)** | Pool Sockets (`Before → Held`) |
|---|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(sdk_5c_rows)}

*(Single-cluster `1,000`-claim reference from Campaign 2 `p2_sdk_1000_cleanup`: `batch.release()` API return = **`{min(sdk1k['batch_release_deletecollection']['release_api_runs_s']):.2f}s` best / `{sdk1k['batch_release_deletecollection']['release_api_med_s']:.2f}s` median** across `3` runs vs `{sdk1k['per_claim_sdk_delete_all']['release_api_med_s']:.2f}s` for `delete_all()` = **`{_sp(sdk1k['per_claim_sdk_delete_all']['release_api_med_s'], min(sdk1k['batch_release_deletecollection']['release_api_runs_s']))}` best / `{_sp(sdk1k['per_claim_sdk_delete_all']['release_api_med_s'], sdk1k['batch_release_deletecollection']['release_api_med_s'])}` median**, and `{sdk1k['per_claim_concurrent_terminate']['release_api_med_s']:.2f}s` median (`{min(sdk1k['per_claim_concurrent_terminate']['release_api_runs_s']):.2f}s–{max(sdk1k['per_claim_concurrent_terminate']['release_api_runs_s']):.2f}s`) for `1,000` concurrent `terminate()` calls under 3-lane APF.)*

---

## 4. Compute Packing Density (`C3-2`), Multi-Step RL (`R2E-Gym` & `GRPO`), and `3,000`-Task `SWE-bench` Sweeps

### 4A. `C3-2` — Single-Cluster Compute Density Sweep (`2.0`, `5.0`, `7.5` Active Sandboxes / vCPU on `320` Worker vCPUs)

| Active Sandboxes (`Density`) | `HEAD per_claim` (`Acquire` · `TTFI P50 / P99` · Sockets) | **`SandboxBatch` `batch64` (`Acquire` · `TTFI P50 / P99` · Sockets)** | Highlights |
|---|---|---|---|
{chr(10).join(dens_rows)}

### 4B. Campaign 2 Multi-Step Synchronous RL (`R2E-Gym` & `Tunix GRPO`) + `10,000`-Claim Soak + `SWE-bench` `3,000` Summary

| Suite & Workload Shape | `HEAD per_claim` | **`SandboxBatch` (`batch`)** | Matched-Cluster Gain |
|---|---:|---:|---|
| **`R2E-Gym` 8-Step Synchronous RL** (`500` imgs × `2` × `8` = `8,000`, `git + 5s`, `c=500`) | `{r2e_pc['variant_wall_s']:.2f}s` (`{r2e_pc['goodput_ok_per_s']:.2f}/s`) · Step `P100`: `138.32s` | **`{r2e_ba['variant_wall_s']:.2f}s` (`{r2e_ba['goodput_ok_per_s']:.2f}/s`)** · Step `P100`: **`28.48s`** | **`3.82×` wall** · **`4.86×` faster steady RL step barrier** (`8,000/8,000` OK) |
| **`Tunix GRPO` Wide Group RL** (`64` imgs × `16` × `8` = `8,192`, `c=1,024`, `Config H` `100/s` cap) | `{grpo_pc['variant_wall_s']:.2f}s` (`{grpo_pc['open_sockets_after']}` post-run socks, Step 0 `51.83s`) | `{grpo_ba['variant_wall_s']:.2f}s` (**`{grpo_ba['open_sockets_after']}` post-run socks**, Step 0 **`43.18s`**) | **`1.20×` faster fresh Step 0** · **`3.88×` fewer post-run sockets** *(Steps 1–7 refill-capped; `per_claim` `1.14×` faster total wall)* |
| **`10,000`-Claim Sustained Wave Soak** (`50` waves × `200` claims in 1 Python process) | `{soak_pc['soak_wall_s']:.2f}s` · `512.0 MB` RSS · `3,017` threads | **`{soak_ba['soak_wall_s']:.2f}s`** · **`320.9 MB` RSS · `1,857` threads** | **`-37.3%` RSS** · **`-38.4%` OS threads** · `+23.7%` peak burst rate (`10,000/10,000` OK) |
| **`SWE-bench` `3,000` — `T0` Stock APF (`naive / sliding / pipelined`)** | `{n_pc['wall_s']:.2f}s / {sl_pc['wall_s']:.2f}s / {pi_pc['wall_s']:.2f}s` (`naive P99`: `{n_pc['acq_p99_s']:.2f}s`) | **`{n_ba['wall_s']:.2f}s / {sl_ba['wall_s']:.2f}s / {pi_ba['wall_s']:.2f}s`** (`naive P99`: **`{n_ba['acq_p99_s']:.2f}s`**) | **`4.62× / 4.88× / 2.66×` faster wall** · **`7.24×` lower `naive` claim `P99`** |
| **`SWE-bench` `3,000` — `T0` In-Place `git restore` Recycle (`max_reuses=6`)** | `{rc_pc['wall_s']:.2f}s` (`392` 30s quarantines) | **`{rc_ba['wall_s']:.2f}s` (`159` quarantines)** | **`1.21×` faster** · fresh `batch` (`87.40s`) beats `git restore` **`4.91×`** |
| **`SWE-bench` `3,000` — `T1` `600 GB` Preloaded SBD (`pipelined` `#33/#34` · `git+30s` `#35/#36` · symmetric `naive` `#39–#42`)** | Hydrated `pipelined`: `{t1p_pc['wall_s']:.2f}s` · `git+30s`: `{t1s_pc['wall_s']:.2f}s` (`794` socks)<br>Sym cold `naive` (`#39`): **`{cs_pc['wall_s']:.2f}s`** · Sym warm `naive` (`#41`): **`{h_pc['wall_s']:.2f}s`** | **Hydrated `pipelined`: `{t1p_ba['wall_s']:.2f}s` · `git+30s`: `{t1s_ba['wall_s']:.2f}s` (`157` socks)**<br>Sym cold `naive` (`#40`): `{cs_ba['wall_s']:.2f}s` · Sym warm `naive` (`#42`): `{h_ba['wall_s']:.2f}s` | **`{_sp(t1p_pc['wall_s'], t1p_ba['wall_s'])}` faster `pipelined` wall** · **`4.50×–5.06×` fewer sockets** · **`22.9 ms/task` infra overhead** *(C2 `#31` `281.65s` → `#32` `139.10s` was sequential first-boot SBD; see §5A)* |

---

## 5. `T1` Secondary Boot Disk Hydration Physics, Workstream B SHA Provenance & Honest Regime Boundaries

### 5A. Symmetric First-Boot Cold SBD (`#39–#40`) & Symmetric Hydrated SBD (`#41–#42`) (`C3-1a`)
- On a newly created `gvisor-pool` backed by `swebench-500-preload-600g` (`600 GB` `CONTAINER_IMAGE_CACHE`), GCE hydrates disk blocks lazily from GCS on first read (`GKE-14`). In both Campaign 2 (`#31` `{t1n_pc['wall_s']:.2f}s` → `#32` `{t1n_ba['wall_s']:.2f}s`) and our initial Campaign 3 diagnostic (`{u_pc['wall_s']:.2f}s` → `{u_ba['wall_s']:.2f}s`, `tgs=-1`), running `per_claim` first and `batch64` second on the same freshly created SBD pool gave `batch64` an asymmetric hydrated-SBD advantage.
- To establish **100% footing-symmetric SBD numbers across both cache states**, `C3-1a` ran two controlled symmetric pairs (`500 × 6 = 3,000` SWE-bench tasks, `c = 500`, `naive`, `tgs=0, cow=True`, `3-lane APF`):
  1. **Symmetric First-Boot Cold SBD (`#39` vs `#40` — brand-new `10 × n2-standard-32 + 600 GB SBD` nodepool created from scratch immediately before each arm, preceded by a non-SWE-bench smoke gate):** `#39` `per_claim` = **`{cs_pc['wall_s']:.2f}s`** (`wait_pool_ready avg = {cs_pc['wpr_avg_s']:.2f}s`, `TTFI P50 = {cs_pc['paired_ttfi_s']['p50']:.2f}s`, `{cs_pc['open_sockets_after']}` sockets) vs `#40` `batch64` = `{cs_ba['wall_s']:.2f}s` (`wait_pool_ready avg = {cs_ba['wpr_avg_s']:.2f}s`, `TTFI P50 = {cs_ba['paired_ttfi_s']['p50']:.2f}s`, **`{cs_ba['open_sockets_after']}` sockets = `{_sp(cs_pc['open_sockets_after'], cs_ba['open_sockets_after'])}` fewer**).
  2. **Symmetric Hydrated SBD (`#41` vs `#42` — back-to-back on fully hydrated SBD blocks):** `#41` `per_claim` = **`{h_pc['wall_s']:.2f}s`** (`wait_pool_ready avg = {h_pc['wpr_avg_s']:.2f}s`, `{h_pc['open_sockets_after']}` sockets) vs `#42` `batch64` = `{h_ba['wall_s']:.2f}s` (`wait_pool_ready avg = {h_ba['wpr_avg_s']:.2f}s`, **`{h_ba['open_sockets_after']}` sockets = `{_sp(h_pc['open_sockets_after'], h_ba['open_sockets_after'])}` fewer**, lower `TTFI First` `{h_ba['paired_ttfi_s']['first']:.2f}s` & `P99` `{h_ba['paired_ttfi_s']['p99']:.2f}s`).

### 5B. Workstream B Driver SHA Provenance (`4018f0dd` → `bb443e71`)
- Campaign 2 used `agent-sandbox-controller@sha256:b0b3c550...` + driver commit `4018f0dd` (`asrl-driver@sha256:eb77db4f...`). Campaign 3 kept the **exact same controller digest** and updated the driver to **`bb443e71`** (`asrl-driver@sha256:6d0b37df...`) for Workstream B correctness hardening (`Lease`-UID selector scoping, full-duration crash-reaper grace, deadline-bounded pool precheck, `pod_annotations`, shared `CoreV1Api`, cold-start watchdog, parallel rollback, auto-sized connection pools, `4,096` executor thread ceiling).
- **Verified Zero Hot-Path Regression:** Hydrated `T1` SBD `batch64` in Campaign 3 (`#42` **`{h_ba['wall_s']:.2f}s`**) matched Campaign 2 `#32` (**`{t1n_ba['wall_s']:.2f}s`**) within **`{parity_pct:.1f}%`**.

### 5C. Regime Boundaries — Where `per_claim` Matches or Beats `batch`
To support accurate engineering decisions, every workload regime where `per_claim` recorded a faster wall/release metric than `batch` is summarized below:
1. **Size-1 Warm Pools (`wp=1`) Under Tuned 3-Lane APF (`C3-1a` `#39` `{cs_pc['wall_s']:.2f}s` vs `#40` `{cs_ba['wall_s']:.2f}s` cold SBD; `#41` `{h_pc['wall_s']:.2f}s` vs `#42` `{h_ba['wall_s']:.2f}s` hydrated SBD; `C2` `Tunix GRPO` `{grpo_pc['variant_wall_s']:.2f}s` vs `{grpo_ba['variant_wall_s']:.2f}s`; `C2` `Bench 3A` `pipelined` `{s3a_pipe_pc['wall_s']:.2f}s` vs `{s3a_pipe_ba['wall_s']:.2f}s`):** When 3-lane APF + `gomaxprocs=16` eliminate HTTP `429` throttling, `per_claim` streams individual `SandboxClaim` `DELETE`s as each task finishes so controller `wp=1` warm-pool refill overlaps wave stragglers, whereas `batch(mif=64, create_rps=200)` paces creation and bulk-deletes at wave boundaries (trading `1.09×–1.50×` wall time for **`3.88×–4.68×` fewer open sockets**). When `pipelined` overlaps waves (`#33` `{t1p_pc['wall_s']:.2f}s` → `#34` `{t1p_ba['wall_s']:.2f}s`) or real task work runs (`#35` `{t1s_pc['wall_s']:.2f}s` → `#36` `{t1s_ba['wall_s']:.2f}s`), `batch64` wins on both wall time and sockets.
2. **Release RPC Return Latency on Tuned Multi-Core Control Planes (`C3-3c` `{c8_pc['release_wall_med_s']:.2f}s` vs `{c8_ba['release_wall_med_s']:.2f}s` at `c=8,000`; `C3-2` `{d24_pc['release_wall_s']:.2f}s` vs `{d24_ba['release_wall_s']:.2f}s` at `c=2,400`):** Issuing `N` concurrent `DELETE`s across `48–240` apiserver vCPUs can return slightly faster or comparably to a single `etcd` `DELETE_COLLECTION` range transaction, while `DELETE_COLLECTION` avoids `500×` API request amplification and beats `AsyncSandboxClient.delete_all()` by **`{_sp(d5000['release_api_med_s'], b5000['release_api_med_s'])}`** (`{d5000['release_api_med_s']:.2f}s → {b5000['release_api_med_s']:.2f}s` at `n=5,000`).

---

## 6. Reproducibility & Companion `RESULTS.md` Ledger Cross-Reference (`#17–#42`)

- **Pinned Digests:**
  - Controller: `us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/agent-sandbox-controller@sha256:b0b3c550ad8eed1571c9494e3dc40b710aff724796cf4671fb1b7a5b959a3716`
  - Driver (Campaign 2, `4018f0dd`): `us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:eb77db4f0a3355d4a00c7671d275bb147c0bfb5b0659b84ce90d4aebe5bd8bcda`
  - Driver (Campaign 3, `bb443e71`): `us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:6d0b37df8c80b092186d5c8ee66d13e646283d48a686301d6ece6c3532f2a8c3`
  - Guest Images (`500` SWE-bench Verified + `500` R2E-Gym): `us-central1-docker.pkg.dev/<GCP_PROJECT>/swebench-mirror/swebench-verified@sha256:5f6cd938e4e220fd63542b44835e767b3c6857ad9e6c9cc669214e34ec3161f0` (+ `499` `@sha256` digests).
- **Companion `REPORT_TEMPLATE.md` Files (`../RESULTS.md` Rows `#17–#42`):**
  - [`brian-nguyen_20261105-073700.md`](./brian-nguyen_20261105-073700.md) — Campaign 2 Phase I `T0` Stock APF SWE-bench `3,000` (`#17–#24`)
  - [`brian-nguyen_20261105-143900.md`](./brian-nguyen_20261105-143900.md) — Campaign 2 Phase II `T0` In-Cluster `Config H` + 3-Lane APF SWE-bench `3,000` (`#25–#30`)
  - [`brian-nguyen_20261105-151500.md`](./brian-nguyen_20261105-151500.md) — Campaign 2 Phase III `T1` `600 GB` Preloaded SBD SWE-bench `3,000` (`#31–#36`)
  - [`brian-nguyen_20261106-154500.md`](./brian-nguyen_20261106-154500.md) — Campaign 3 `C3-1b` `T0` `0%`-Cached Cold GCFS SWE-bench `500×1` (`#37–#38`)
  - [`brian-nguyen_20261106-163800.md`](./brian-nguyen_20261106-163800.md) — Campaign 3 `C3-1a` `T1` `600 GB` SBD First-Boot Hydration & Hydrated Parity SWE-bench `3,000` (`#39–#42`)
"""


def render_pr5_body(s: dict) -> str:
    c2 = s["campaign_2"]
    c3 = s["campaign_3"]
    tot3 = s["campaign_3_totals"]
    c3_3d = c3["c3_3d_mc5_sdk_5000"]
    assert c3_3d is not None

    t1p_pc = c2["phase3_t1_sbd_3000"]["pipelined"]["per_claim"]["variants"][0]
    t1p_ba = c2["phase3_t1_sbd_3000"]["pipelined"]["batch64"]["variants"][0]
    b_pc = c3["c3_1b_cold_gcfs_500"]["per_claim"]["variants"][0]
    b_ba = c3["c3_1b_cold_gcfs_500"]["batch64"]["variants"][0]
    cs_pc = c3["c3_1a_cold_sbd_symmetric"]["per_claim"]["variants"][0]
    cs_ba = c3["c3_1a_cold_sbd_symmetric"]["batch64"]["variants"][0]
    h_pc = c3["c3_1a_anchor_hydrated_exact_c2"]["per_claim"]["variants"][0]
    h_ba = c3["c3_1a_anchor_hydrated_exact_c2"]["batch64"]["variants"][0]
    w1_pc_1k = _pt_by_c(c3["c3_1c_warm_ttfi_1x1000"]["per_claim"], 1000)
    w1_ba_1k = _pt_by_c(c3["c3_1c_warm_ttfi_1x1000"]["batch64"], 1000)
    d24_pc = _pt_by_c(c3["c3_2_density"]["per_claim"], 2400)
    d24_ba = _pt_by_c(c3["c3_2_density"]["batch64"], 2400)
    gs_pc = c3["c3_3b_mc5_golden_small"]["per_claim"]
    gs_ba = c3["c3_3b_mc5_golden_small"]["batch20"]
    c8_pc = c3["c3_3c_mc5_cuj1_8000"]["per_claim"]["points"][0]
    c8_ba = c3["c3_3c_mc5_cuj1_8000"]["batch64"]["points"][0]
    b5000 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 5000)
    p5000 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 5000)
    d5000 = _c3_3d_row(c3_3d, "per_claim_sdk_delete_all", 5000)

    grand_ops = 148544 + tot3["total_sandbox_operations"]

    return f"""## Results PR

Adds Campaign 2 (`#17–#36`) and Campaign 3 (`#37–#42`) head-to-head `SandboxBatch` (`claim_mode="batch"`) vs `HEAD per_claim` (`claim_mode="per_claim"`) GKE benchmark results across **`{grand_ops:,}` real gVisor sandbox claims and multi-step RL trajectories** (`57` total benchmark runs across `1` and `5` regional GKE `v1.37.1-gke.1697000` clusters; **`0` claim errors across every `SandboxBatch` run**, `0` controller restarts).

### Files Included (Results-Only PR)
- `agent-sandbox/RESULTS.md` — appends `26` SWE-bench ledger rows (`#17–#42`):
  - `#17–#24` (`brian-nguyen_20261105-073700.md`): Campaign 2 Phase I `T0` stock GKE APF (`500×6 = 3,000`, `naive`, `sliding`, `pipelined`, `naive+recycle`).
  - `#25–#30` (`brian-nguyen_20261105-143900.md`): Campaign 2 Phase II `T0` in-cluster `Config H` (`100 pods/s` refill cap) + 3-lane APF (`500×6 = 3,000`).
  - `#31–#36` (`brian-nguyen_20261105-151500.md`): Campaign 2 Phase III `T1` `600 GB` preloaded Secondary Boot Disk (`naive`, `pipelined` infra-floor, and `pipelined` real-work `git + sleep 30`).
  - `#37–#38` (`brian-nguyen_20261106-154500.md`): Campaign 3 `C3-1b` `T0` genuinely cold `0%`-cached GCFS streaming (`500×1 = 500`, `10 × e2-standard-32` `200 GB pd-balanced` `gvisor-pool` recreated fresh before each arm, `probe='python3 -V'`).
  - `#39–#42` (`brian-nguyen_20261106-163800.md`): Campaign 3 `C3-1a` `T1` `600 GB` SBD **symmetric first-boot cold SBD (`#39–#40`, `tgs=0, cow=True`, fresh `10 × n2-standard-32 + 600 GB SBD` pool recreated before each arm)** and **symmetric hydrated SBD (`#41–#42`, `tgs=0, cow=True`, verifying Workstream B SHA `4018f0dd → bb443e71` within `5.1%` of `#32`)**.
- `agent-sandbox/performance_reports/11_05_2026_RESULTS.md` — standalone synthesis report covering all `36` Campaign 2 + `21` Campaign 3 suites (including multi-step `R2E-Gym` & `Tunix GRPO` RL loops, true warm `TTFI` curves, `7.5` sandboxes/vCPU packing density, `10,000`-claim soak, and 5-cluster `4,000` / `4,096` / `5,000` / `8,000` scale tests).

### Headline Verified Results

| Benchmark Suite | `HEAD per_claim` | **`SandboxBatch` (`batch`)** | Verified Comparison |
|---|---:|---:|---|
| **`T0` SWE-bench `3,000` (`#17` vs `#21` `naive`, `#18` vs `#22` `sliding`)** | `403.63s` / `433.13s` (`P99`: `139.66s`) | **`87.40s` / `88.76s`** (`P99`: **`19.30s`**) | **`4.62×` / `4.88×` faster wall · `7.24×` lower `P99`** |
| **`T0` Cold `0%`-Cached GCFS `500×1` (`#37` vs `#38`)** | `{b_pc['wall_s']:.2f}s` (`TTFI P50`: `{b_pc['paired_ttfi_s']['p50']:.2f}s`) | **`{b_ba['wall_s']:.2f}s`** (`TTFI P50`: **`{b_ba['paired_ttfi_s']['p50']:.2f}s`**) | **`{_sp(b_pc['wall_s'], b_ba['wall_s'])}` faster cold pull** (fresh `e2-standard-32` pool per arm) |
| **`T1` `600 GB` SBD `3,000` (`#33/#34` `pipelined`; `#39/#40` sym cold `naive`; `#41/#42` sym warm `naive`)** | `pipelined`: `{t1p_pc['wall_s']:.2f}s`<br>Sym cold (`#39`): **`{cs_pc['wall_s']:.2f}s`** (`559` socks)<br>Sym warm (`#41`): **`{h_pc['wall_s']:.2f}s`** (`524` socks) | **`pipelined`: `{t1p_ba['wall_s']:.2f}s`**<br>Sym cold (`#40`): `{cs_ba['wall_s']:.2f}s` (**`124` socks**)<br>Sym warm (`#42`): `{h_ba['wall_s']:.2f}s` (**`112` socks**) | **`{_sp(t1p_pc['wall_s'], t1p_ba['wall_s'])}` faster `pipelined`**; on `wp=1` `naive` + 3-lane APF, `per_claim` streams deletes `1.09×–1.17×` faster while `batch` cuts sockets **`4.51×–4.68×`** |
| **`T1` SBD Real-Work `git + 30s` (`#35` vs `#36`, `180s` sleep floor)** | `257.44s` (`794` sockets) | **`248.84s`** (**`157` sockets**) | **`5.06×` fewer sockets · `22.9 ms/task` amortized infra overhead** |
| **`R2E-Gym` 8-Step Sync RL (`8,000` tasks, `c=500`)** | `1,494.54s` (Step `P100`: `138.32s`) | **`390.77s`** (Step `P100`: **`28.48s`**) | **`3.82×` wall · `4.86×` faster steady RL step barrier** |
| **1-Cluster `1,000`-Pod True Warm `TTFI` (`C3-1c` `1×1000`, `c=1,000` med)** | `P50={w1_pc_1k['p50_med_s']:.2f}s` / `P99={w1_pc_1k['p99_med_s']:.2f}s` | **`P50={w1_ba_1k['p50_med_s']:.2f}s` / `P99={w1_ba_1k['p99_med_s']:.2f}s`** | **`{_sp(w1_pc_1k['p50_med_s'], w1_ba_1k['p50_med_s'])}` faster `TTFI P50` · `{_sp(w1_pc_1k['p99_med_s'], w1_ba_1k['p99_med_s'])}` lower `TTFI P99`** |
| **1-Cluster Packing Density (`C3-2`, `c=2,400` = `7.5` sandboxes/vCPU)** | `{d24_pc['acquire_wall_s']:.2f}s` acq · `TTFI P50={d24_pc['ttfi_s']['p50']:.2f}s` | **`{d24_ba['acquire_wall_s']:.2f}s` acq · `TTFI P50={d24_ba['ttfi_s']['p50']:.2f}s`** | **`{_sp(d24_pc['ttfi_s']['p50'], d24_ba['ttfi_s']['p50'])}` faster `TTFI P50` · `0` OOMs / evictions** |
| **5-Cluster `8 × 512 = 4,096` Multi-Job RL Step (`C3-3b`)** | `{gs_pc['all_jobs_wall_s']:.2f}s` (Job `P100`: `{gs_pc['job_step_barrier_s']['max']:.2f}s`) | **`{gs_ba['all_jobs_wall_s']:.2f}s`** (Job `P100`: **`{gs_ba['job_step_barrier_s']['max']:.2f}s`**) | **`{_sp(gs_pc['all_jobs_wall_s'], gs_ba['all_jobs_wall_s'])}` wall · `{_sp(gs_pc['job_step_barrier_s']['max'], gs_ba['job_step_barrier_s']['max'])}` lower job `P100` barrier** |
| **5-Cluster `8,000`-Sandbox Single Wave (`C3-3c`)** | `{c8_pc['acquire_wall_med_s']:.2f}s` acq · `{int(c8_pc['held_open_sockets_med']):,}` socks | **`{c8_ba['acquire_wall_med_s']:.2f}s` acq · `{int(c8_ba['held_open_sockets_med']):,}` socks** | **`{_sp(c8_pc['acquire_wall_med_s'], c8_ba['acquire_wall_med_s'])}` faster acquire · `{_sp(c8_pc['held_open_sockets_med'], c8_ba['held_open_sockets_med'])}` fewer sockets** |
| **5-Cluster `5,000`-Sandbox Pure SDK Release (`C3-3d`)** | `delete_all`: `{d5000['release_api_med_s']:.2f}s` API / `{d5000['pods_gone_med_s']:.2f}s` pods gone (`12` acq err)<br>conc `terminate`: `{p5000['release_api_med_s']:.2f}s` API / `{p5000['pods_gone_med_s']:.2f}s` pods gone (`13` acq err) | **`batch.release()`: `{b5000['release_api_med_s']:.2f}s` API / `{b5000['pods_gone_med_s']:.2f}s` pods gone (`0` err)** | **`{_sp(d5000['release_api_med_s'], b5000['release_api_med_s'])}` faster API return (`{_sp(d5000['pods_gone_med_s'], b5000['pods_gone_med_s'])}` faster `pods_gone_s`) vs `delete_all()` · `{_sp(p5000['release_api_med_s'], b5000['release_api_med_s'])}` faster API (`{_sp(p5000['pods_gone_med_s'], b5000['pods_gone_med_s'])}` `pods_gone_s`) vs conc `terminate()`** |

### Key Methodology & Comparability Caveats
- **Infra-floor (`probe='true'`) vs cold-interpreter (`probe='python3 -V'`) vs real-work (`#1` / `#35–#36`):** Rows `#17–#34` and `#39–#42` use `probe='true'` (infra floor) and `#37–#38` use `probe='python3 -V'` on `500×1=500` tasks — neither is directly comparable on `s/task` to `#1` (`2,218.10s` real-work record). Only `#35–#36` (`git status + git log + sleep 30`, `180s` 6-wave sleep floor) exercise sustained task work, and `#35–#36` used `n2-standard-32` + `ctrl=1000/500` (required by stock GKE for `secondaryBootDisks`) rather than `#1`'s `e2` + `ctrl=1000/50`.
- **Symmetric vs sequential first-boot SBD hydration (`#31–#32` vs `#39–#42`):** Campaign 2's `#31` (`281.65s`) ran first on a newly created SBD nodepool (`wait_pool_ready avg=37.24s`) while `#32` (`139.10s`) ran second on hydrated blocks (`8.46s`). Campaign 3 rows `#39–#40` (`{cs_pc['wall_s']:.2f}s` vs `{cs_ba['wall_s']:.2f}s`) recreate a brand-new `10 × n2-standard-32 + 600 GB SBD` nodepool before **each** arm for symmetric first-boot cold SBD comparison, and `#41–#42` (`{h_pc['wall_s']:.2f}s` vs `{h_ba['wall_s']:.2f}s`) measure symmetric fully hydrated SBD.
- **Driver SHA note (`4018f0dd → bb443e71`):** Campaign 3 kept the identical controller digest (`@sha256:b0b3c550...`) and updated the Python driver to `bb443e71` (`@sha256:6d0b37df...`) for Workstream B correctness hardening; `#42` (`{h_ba['wall_s']:.2f}s`) matched Campaign 2 `#32` (`139.10s`) within `5.1%`.
"""


def render_gke_13_16_block(s: dict) -> str:
    c3 = s["campaign_3"]
    c3_3d = c3["c3_3d_mc5_sdk_5000"]
    assert c3_3d is not None

    cs_pc = c3["c3_1a_cold_sbd_symmetric"]["per_claim"]["variants"][0]
    cs_ba = c3["c3_1a_cold_sbd_symmetric"]["batch64"]["variants"][0]
    u_pc = c3["c3_1a_anchor_unhydrated_sbd"]["per_claim"]["variants"][0]
    u_ba = c3["c3_1a_anchor_unhydrated_sbd"]["batch64"]["variants"][0]
    h_pc = c3["c3_1a_anchor_hydrated_exact_c2"]["per_claim"]["variants"][0]
    h_ba = c3["c3_1a_anchor_hydrated_exact_c2"]["batch64"]["variants"][0]
    b5000 = _c3_3d_row(c3_3d, "batch_release_deletecollection", 5000)
    p5000 = _c3_3d_row(c3_3d, "per_claim_concurrent_terminate", 5000)
    d5000 = _c3_3d_row(c3_3d, "per_claim_sdk_delete_all", 5000)

    return f"""<a id="gke-13"></a>

## GKE-13 Use `501–1,000` node bucket (`go/kcp-resize`) and `pd-standard` OS disks to preserve regional `SSD_TOTAL_GB` quota for Secondary Boot Disks

**Takeaway:** GKE regional master VM sizing (`go/kcp-resize`) scales in discrete node buckets. Creating an initial temporary Spot warmup pool above `500` total nodes (`170` nodes/zone × `3` zones = `510` nodes) sizes regional KCP to `gomaxprocs = 16` (`1,449` nominal APF seats) immediately on birth. Using `--disk-type=pd-standard` for temporary warmup pools and worker OS boot disks consumes `0 GB` of `SSD_TOTAL_GB`, reserving the regional `40,960 GB` SSD quota exclusively for `50 × 600 GB = 30,000 GB` Secondary Boot Disks.

<details>
<summary>The full story (Campaign 3 5-Cluster Fleet, provision_multicluster_c3.sh & provision_all5_c3_rerun.sh)</summary>

- **Situation:** Multi-cluster testing across `5` regional GKE clusters (`asrl-c3-1..5`) required `50 × n2-standard-32` worker nodes backed by `600 GB` `swebench-500-preload-600g` Secondary Boot Disks (`30,000 GB` total `pd-ssd`).
- **What goes wrong:** If temporary master-warmup Spot pools or worker OS boot disks use `pd-balanced`, GCP charges them against regional `SSD_TOTAL_GB` (`40,960 GB` limit in `us-central1`), causing `gvisor-pool` creation to fail with `Quota SSD_TOTAL_GB exceeded`.
- **What we do:** In `provision_multicluster_c3.sh` and `provision_all5_c3_rerun.sh` (`--num-nodes=170` × `3` zones = `510` nodes), the temporary Spot warmup pool used `--disk-type=pd-standard --disk-size=30` (and `gvisor-pool`, `ctrl-pool`, and `driver-pool` primary OS boot disks used `100 GB pd-standard`), sizing `kube-apiserver` to `go_sched_gomaxprocs_threads = 16` on all `5` clusters while consuming `0 GB` of `SSD_TOTAL_GB` for OS boot disks.
- **Related:** [GKE-1](#gke-1).

</details>

<a id="gke-14"></a>

## GKE-14 GCE Secondary Boot Disk images hydrate blocks lazily from GCS on first read — recreate SBD nodepools per arm for cold-SBD tests, and account for `wp=1` streaming deletes vs wave-boundary `DELETE_COLLECTION`

**Takeaway:** Attaching a custom GCE disk image as a `CONTAINER_IMAGE_CACHE` Secondary Boot Disk pays lazy GCS block-hydration page faults (`wait_pool_ready avg = {cs_pc['wpr_avg_s']:.2f}s–{cs_ba['wpr_avg_s']:.2f}s`) on the first run after node creation before dropping `>20×` (`{h_pc['wpr_avg_s']:.2f}s–{h_ba['wpr_avg_s']:.2f}s`) once hydrated. Never compare a first-boot run against a second run on the same SBD pool; recreate the SBD nodepool from scratch before each arm for cold-SBD comparisons. On `wp=1` `naive` waves when 3-lane APF + `gomaxprocs=16` eliminate HTTP `429`s, `per_claim` finishes `1.09×` faster on symmetric cold SBD (`{cs_pc['wall_s']:.2f}s` vs `{cs_ba['wall_s']:.2f}s`) and `1.17×` faster on symmetric hydrated SBD (`{h_pc['wall_s']:.2f}s` vs `{h_ba['wall_s']:.2f}s`) because streaming individual `DELETE`s start controller refill before wave stragglers finish, while `batch64` cuts open sockets `4.51×–4.68×` (`{cs_pc['open_sockets_after']} → {cs_ba['open_sockets_after']}` and `{h_pc['open_sockets_after']} → {h_ba['open_sockets_after']}`).

<details>
<summary>The full story (Campaign 3 C3-1a Symmetric Cold & Hydrated SBD, RESULTS.md #39–#42)</summary>

- **Situation:** `C3-1a` ran `500 × 6 = 3,000` SWE-bench tasks (`naive`, `probe='true'`) on `10 × n2-standard-32` nodes with `600 GB` `swebench-500-preload-600g` SBD.
- **What goes wrong:** In an initial sequential diagnostic (`tgs=-1`), Run 1 (`per_claim`, `wall = {u_pc['wall_s']:.2f}s`, `wait_pool_ready avg = {u_pc['wpr_avg_s']:.2f}s`) pulled SBD blocks lazily from GCS while Run 2 (`batch64`, `wall = {u_ba['wall_s']:.2f}s`, `wait_pool_ready avg = {u_ba['wpr_avg_s']:.2f}s`) inherited partially hydrated blocks — the same sequential order that inflated Campaign 2 `#31` (`281.65s`) vs `#32` (`139.10s`).
- **What we do:** First, `run_c3_1a_symmetric_cold_sbd.sh` deleted and recreated a brand-new `10 × n2-standard-32 + 600 GB SBD` `gvisor-pool` before **each** arm (`tgs=0, cow=True`, non-SWE-bench smoke gate): `#39` `per_claim` (`{cs_pc['wall_s']:.2f}s`, `wpr avg = {cs_pc['wpr_avg_s']:.2f}s`, `{cs_pc['open_sockets_after']}` sockets) vs `#40` `batch64` (`{cs_ba['wall_s']:.2f}s`, `wpr avg = {cs_ba['wpr_avg_s']:.2f}s`, `{cs_ba['open_sockets_after']}` sockets). Second, running both arms back-to-back after full SBD hydration (`#41` `per_claim` `{h_pc['wall_s']:.2f}s` vs `#42` `batch64` `{h_ba['wall_s']:.2f}s`) reproduced Campaign 2 `#32` (`139.10s`) within `5.1%` and confirmed the `wp=1` streaming-delete vs wave-boundary `DELETE_COLLECTION` tradeoff across both cache states.
- **Related:** [GKE-4](#gke-4).

</details>

<a id="gke-15"></a>

## GKE-15 Do not set `resources.requests["ephemeral-storage"]` when `100 GB` COS primary OS boot disks host kubelet overhead and SBD hosts container images

**Takeaway:** Specifying `resources.requests["ephemeral-storage"]: 4Gi` on guest sandboxes when primary COS OS boot disks are `100 GB pd-standard` (`~47 GiB` allocatable `ephemeral-storage` after GKE system/image-gc/eviction reservations) caps scheduling at `10` guest pods/node (`100` pods across a `10`-node cluster). Keep `emptyDir.sizeLimit: 4Gi` on `/tmp` without setting `requests["ephemeral-storage"]`, and verify `--node-selector` matches the actual pool label (`cloud.google.com/gke-nodepool=gvisor-pool`).

<details>
<summary>The full story (Campaign 3 C3-3d warm-pool scheduling failures #4 and #8)</summary>

- **Situation:** `C3-3d` stood up `1,000` warm gVisor SWE-bench pods per cluster (`100` pods/node) across `5` clusters with `100 GB pd-standard` primary boot disks + `600 GB` SBD image caches.
- **What goes wrong:** Requesting `ephemeral-storage: 4Gi` caused `kube-scheduler` to stall warm pools at exact `100/1000` ready replicas per cluster (`Insufficient ephemeral-storage`); later passing `--node-selector bench-role=worker` (absent on `gvisor-pool`) left `0/1000` pods scheduled for `600s`.
- **What we do:** Omit `resources.requests["ephemeral-storage"]` (retaining `volumes.emptyDir.sizeLimit: 4Gi`) and pass `--node-selector cloud.google.com/gke-nodepool=gvisor-pool`. All `5,000` warm gVisor pods (`1,000/cluster` across `5` clusters) scheduled and reached `Ready` in `{c3_3d['setup_s']:.2f}s`.
- **Related:** [GKE-5](#gke-5).

</details>

<a id="gke-16"></a>

## GKE-16 Provision multiple Cloud NAT external IPs, extend `aiohttp` `keepalive_timeout` (`900s`), retry cold TCP/TLS handshakes, and measure both `release_api_s` and `SandboxClaim`-filtered `pods_gone_s` in multi-cluster drivers

**Takeaway:** When an in-cluster driver on private nodes (`asrl-c3-1`) drives remote GKE clusters (`asrl-c3-2..5`) via their external control-plane VIPs, `aiohttp`'s default `15.0s` `keepalive_timeout` closes pooled TLS sockets during `20–30s` warm-pool refills, and `per_claim` watches close response sockets on `Watch.stop()`. Re-opening hundreds of sockets through **1** Cloud NAT IP triggers external L4 load-balancer SYN drops or resets (`31s–122s` TCP SYN retransmit stalls and `ClientConnectorError`s because `aiohttp` does not retry failed TCP `connect()`s). Attach `8` static external IPs to Cloud NAT, set `_keepalive_timeout = 900.0` + `5ms`-paced idempotent `_create_direct_connection` retries (`6.0s` per attempt, picking a fresh ephemeral source port before any HTTP bytes are sent), and track `pods_gone_s` alongside `release_api_s` by filtering strictly to `SandboxClaim`-adopted sandboxes (never unclaimed `SandboxWarmPool`-owned sandboxes).

<details>
<summary>The full story (Campaign 3 C3-3d root cause & verified 5,000-sandbox rerun)</summary>

- **Situation:** `C3-3d` drove `1,000`, `2,500`, and `5,000` simultaneous claims and bulk releases across `5` regional GKE clusters from one Python `asyncio` loop on `asrl-c3-1`.
- **What goes wrong:** Launches #5–#7 suffered `31.3s` and `122.3s` stalls before crashing with `aiohttp` `TimeoutError` because all `~320` idle `kubernetes_asyncio` sockets expired after `15s` during pool refills and burst out of a single Cloud NAT IP; even with `8` NAT IPs, a single dropped SYN or L4 LB reset during `per_claim`'s `O(N)` watch reconnects raises `ClientConnectorError` (`aiohttp` does not retry failed TCP connects, and `AsyncSandboxClient.delete_sandbox` logs and swallows the error). In addition, measuring only `release()` API return overstates real pod cleanup speed, while naively polling `ownerReferences.kind == "Sandbox"` accidentally waits on unclaimed warm-pool pods (which are also owned by `Sandbox` CRs whose owner is `SandboxWarmPool`).
- **What we do:** Attach `8` static regional external IPs (`asrl-c3-nat-ip-1..8`, `--max-ports-per-vm=32768`, `--min-ports-per-vm=4096`) to `asrl-c3-nat`, set `conn._keepalive_timeout = 900.0`, `ClientTimeout(total=300, connect=30, sock_connect=10, sock_read=180)`, and retry `_create_direct_connection` up to `5` times (`6.0s` timeout + `_SynPacer(0.005)`, 100% idempotent since zero HTTP bytes have been sent), and snapshot claimed pod names strictly from `SandboxClaim.status.sandbox.name` / `Sandbox.ownerReferences[kind=="SandboxClaim"]`. Across all `6` `SandboxBatch` bursts (`17,000 / 17,000` claims), `batch` kept its `{int(b5000['socks_before_med'])} → {int(b5000['held_open_sockets_med'])}` warm keep-alive pool intact with **`0` errors**, whereas `per_claim`'s `Watch.stop()` socket closes drained its pool from `{int(p5000['socks_before_med'])} → {int(p5000['held_open_sockets_med'])}` sockets and still incurred `5/2,500` and `12–13/5,000` external GKE VIP `ConnectionTimeoutError`s (because GCP Cloud NAT `ENDPOINT_TYPE_VM` hashes `(driver VM IP, dest VIP, 443)` to one NAT IP per target cluster). At `n = 5,000`, `batch.release()` returned in `{b5000['release_api_med_s']:.2f}s` (`{b5000['release_api_per_s_med']:,.1f}/s`, with `{int(b5000['pods_alive_at_api_return_med']):,}/5,000` pods still alive at API return) vs `{p5000['release_api_med_s']:.2f}s` on concurrent `terminate()` and `{d5000['release_api_med_s']:.2f}s` on `delete_all()`, while full deletion of all `5,000` claimed gVisor pods (`pods_gone_s`) converged in `{b5000['pods_gone_med_s']:.2f}s` (`{b5000['pods_gone_per_s_med']:.1f} pods/s`) on `batch`, `{p5000['pods_gone_med_s']:.2f}s` (`{p5000['pods_gone_per_s_med']:.1f} pods/s`) on concurrent `terminate()`, and `{d5000['pods_gone_med_s']:.2f}s` (`{d5000['pods_gone_per_s_med']:.1f} pods/s`) on `delete_all()`.
- **Related:** [GKE-11](#gke-11).

</details>
"""


def update_gke_benchmarking_notes(notes_file: str, gke_block: str) -> None:
    txt = open(notes_file, encoding="utf-8").read()
    start_marker = '<a id="gke-13"></a>'
    end_marker = "---\n\n## Benchmark harness and measurement engineering"
    assert start_marker in txt and end_marker in txt, "Markers not found in gke-benchmarking.md"
    pre, rest = txt.split(start_marker, 1)
    _, post = rest.split(end_marker, 1)
    new_txt = pre + gke_block + "\n" + end_marker + post
    with open(notes_file, "w", encoding="utf-8") as fh:
        fh.write(new_txt)


def main():
    summary = json.load(open(SUMMARY_JSON, encoding="utf-8"))
    if summary["campaign_3"].get("c3_3d_mc5_sdk_5000") is None:
        print("ERROR: c3_3d_mc5_sdk_5000 is still null in verified_metrics_summary.json!", file=sys.stderr)
        sys.exit(1)

    rl_gke_readme = "<HOME>/agent-sandbox/.claude/worktrees/rl-gke/examples/agent-sandbox-rl/bench-results/gke/campaign-v3/README.md"
    wt_pr5_results = "<SCRATCH_DIR>/wt_pr5/agent-sandbox/performance_reports/11_05_2026_RESULTS.md"
    pr5_body_out = "<SCRATCH_DIR>/fresh_pr5_body.md"
    notes_file = "<HOME>/agent-sandbox-notes/batch-primitives/handoff/learnings/gke-benchmarking.md"

    with open(rl_gke_readme, "w", encoding="utf-8") as fh:
        fh.write(render_campaign_v3_readme(summary))
    print(f"Wrote fresh {rl_gke_readme}")

    with open(wt_pr5_results, "w", encoding="utf-8") as fh:
        fh.write(render_11_05_results_md(summary))
    print(f"Wrote fresh {wt_pr5_results}")

    with open(pr5_body_out, "w", encoding="utf-8") as fh:
        fh.write(render_pr5_body(summary))
    print(f"Wrote fresh {pr5_body_out}")

    update_gke_benchmarking_notes(notes_file, render_gke_13_16_block(summary))
    print(f"Updated GKE-13..16 in {notes_file}")


if __name__ == "__main__":
    main()
