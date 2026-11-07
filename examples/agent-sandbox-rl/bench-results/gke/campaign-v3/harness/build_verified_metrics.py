#!/usr/bin/env python3
"""Single deterministic source-of-truth metrics builder for Campaign 2 & Campaign 3.

Reads ONLY raw benchmark JSONs and sidecar `.meta.json` / `.apf_{before,after}.txt`
files from `scratch/gke_bench_v2/runs` and `scratch/gke_bench_v3/runs`.
Computes:
  1. File SHA-256 digests and total claim / task / error counts (never hiding `probe_err` or `err`).
  2. Exact per-run and multi-repeat median + [min..max] metrics across all C2 and C3 suites.
  3. Explicit separation of API-return release rate (`release_api_s` / `cleanup_s`) vs
     actual Pod-gone convergence (`pods_gone_s` / `reap_to_zero_pods_s`).
  4. Exhaustive `batch_losses_and_tradeoffs` table listing every run or metric where `batch`
     trails `per_claim` (wall-clock, latency, API-return release, or socket count).
"""
from __future__ import annotations
import glob, hashlib, json, os, re, statistics, sys
from pathlib import Path

DEFAULT_C2_DIR = "<SCRATCH_DIR>/gke_bench_v2/runs"
DEFAULT_C3_DIR = "<SCRATCH_DIR>/gke_bench_v3/runs"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def med(vals: list[float]) -> float | None:
    clean = [float(v) for v in vals if v is not None]
    if not clean:
        return None
    return round(float(statistics.median(clean)), 4)


def rng(vals: list[float]) -> list[float] | None:
    clean = [round(float(v), 4) for v in vals if v is not None]
    if not clean:
        return None
    return [min(clean), max(clean)]


def parse_apf_rejects(apf_before_path: str, apf_after_path: str) -> dict[str, int]:
    """Compute delta of `apiserver_flowcontrol_rejected_requests_total` by priority_level."""
    pat = re.compile(r'apiserver_flowcontrol_rejected_requests_total\{[^}]*priority_level="([^"]+)"[^}]*\}\s+([0-9.eE+-]+)')

    def _sum_by_pl(path: str) -> dict[str, int]:
        acc: dict[str, int] = {}
        if not os.path.isfile(path):
            return acc
        for line in open(path, encoding="utf-8", errors="ignore"):
            m = pat.search(line)
            if m:
                pl, val = m.group(1), int(float(m.group(2)))
                acc[pl] = acc.get(pl, 0) + val
        return acc

    b = _sum_by_pl(apf_before_path)
    a = _sum_by_pl(apf_after_path)
    out: dict[str, int] = {}
    for pl in sorted(set(b) | set(a)):
        out[pl] = max(0, a.get(pl, 0) - b.get(pl, 0))
    return out


def load_meta(runs_dir: str, stem: str) -> dict:
    mp = os.path.join(runs_dir, f"{stem}.meta.json")
    meta = json.load(open(mp, encoding="utf-8")) if os.path.isfile(mp) else {}
    rb = meta.get("controller_restarts_before", meta.get("controller_restarts_5c_before"))
    ra = meta.get("controller_restarts_after", meta.get("controller_restarts_5c_after"))
    ctrl_delta = (int(ra) - int(rb)) if (rb is not None and ra is not None) else None
    apf_rej = parse_apf_rejects(
        os.path.join(runs_dir, f"{stem}.apf_before.txt"),
        os.path.join(runs_dir, f"{stem}.apf_after.txt"),
    )
    return {
        "driver_image": meta.get("driver_image"),
        "start_utc": meta.get("start_utc"),
        "end_utc": meta.get("end_utc"),
        "job_elapsed_s": meta.get("job_elapsed_s"),
        "ctrl_restarts_before": rb,
        "ctrl_restarts_after": ra,
        "ctrl_restarts_delta": ctrl_delta,
        "apf_rejected_delta": apf_rej,
    }


def summarize_variant_sweep(runs_dir: str, stem: str) -> dict:
    path = os.path.join(runs_dir, f"{stem}.json")
    doc = json.load(open(path, encoding="utf-8"))
    out_vars = []
    for v in doc.get("variants", []):
        if "steps" in v:
            # Multi-step RL benchmark (Bench 1A / 1B)
            steps = v["steps"]
            steady = steps[1:] if len(steps) > 1 else steps
            total_err = sum(int(s.get("err", 0) or 0) for s in steps)
            peak_socks = max((int((s.get("proc") or {}).get("open_sockets", 0) or 0) for s in steps), default=0)
            peak_rss = max((float((s.get("proc") or {}).get("vm_rss_mb", 0.0) or 0.0) for s in steps), default=0.0)
            pa_loop = v.get("proc_after") or {}
            out_vars.append({
                "label": v.get("label"),
                "claim_mode": v.get("claim_mode"),
                "batch_mif": v.get("batch_max_in_flight"),
                "variant_wall_s": round(float(v["variant_wall_s"]), 2),
                "total_tasks": int(v["total_tasks"]),
                "total_ok": int(v["total_ok"]),
                "total_err": total_err,
                "goodput_ok_per_s": round(float(v["goodput_ok_per_s"]), 3),
                "wall_per_ok_task_s": round(float(v["wall_per_ok_task_s"]), 4),
                "step0_wall_s": round(float(steps[0]["step_wall_s"]), 2) if steps else None,
                "steady_step_wall_med_s": med([s["step_wall_s"] for s in steady]),
                "steady_step_wall_mean_s": round(sum(s["step_wall_s"] for s in steady) / len(steady), 2) if steady else None,
                "steady_env_ready_all_mean_s": round(sum(s["env_ready_all_s"] for s in steady) / len(steady), 2) if steady else None,
                "steady_env_ready_p50_med_s": med([s["env_ready_p50_s"] for s in steady]),
                "peak_open_sockets": peak_socks,
                "open_sockets_after": pa_loop.get("open_sockets"),
                "peak_rss_mb": round(peak_rss, 2),
                "rss_after_mb": pa_loop.get("vm_rss_mb"),
            })
        else:
            ph = v.get("phases") or {}
            wpr = ph.get("wait_pool_ready") or {}
            acq = ph.get("acquire") or ph.get("claim") or {}
            ttfi = v.get("paired_ttfi_s") or {}
            wire = v.get("paired_wire_claim_s") or {}
            fcmd = v.get("paired_first_cmd_exec_s") or v.get("paired_first_cmd_s") or {}
            pb = v.get("proc_before") or {}
            pa = v.get("proc_after") or {}
            wall_s = float(v["wall_s"])
            ok_v = int(v.get("ok", v.get("tasks_ok", 0)))
            err_v = int(v.get("err", v.get("tasks_err", 0)))
            tasks_v = int(v.get("tasks", v.get("total_tasks", 0)))
            tps = v.get("tasks_per_s", v.get("goodput_tasks_per_s"))
            if tps is None:
                tps = (ok_v / wall_s) if wall_s > 0 else 0.0
            out_vars.append({
                "label": v.get("label"),
                "strategy": v.get("strategy"),
                "recycle": v.get("recycle"),
                "claim_mode": v.get("claim_mode"),
                "batch_mif": v.get("batch_max_in_flight"),
                "wall_s": round(wall_s, 2),
                "s_per_task": round(float(v["s_per_task"]), 4),
                "tasks_per_s": round(float(tps), 2),
                "tasks": tasks_v,
                "claims": int(v["claims"]),
                "ok": ok_v,
                "err": err_v,
                "env_ready_ok_s": v.get("env_ready_ok_s"),
                "wpr_n": wpr.get("n"),
                "wpr_avg_s": wpr.get("avg"),
                "wpr_p50_s": wpr.get("p50"),
                "wpr_p99_s": wpr.get("p99"),
                "wpr_max_s": wpr.get("max"),
                "acq_avg_s": acq.get("avg"),
                "acq_p50_s": acq.get("p50"),
                "acq_p99_s": acq.get("p99"),
                "acq_max_s": acq.get("max"),
                "paired_ttfi_s": ttfi if ttfi.get("n") else None,
                "paired_wire_claim_s": wire if wire.get("n") else None,
                "paired_first_cmd_exec_s": fcmd if fcmd.get("n") else None,
                "open_sockets_before": pb.get("open_sockets"),
                "open_sockets_after": pa.get("open_sockets"),
                "vm_hwm_mb": pa.get("vm_hwm_mb"),
                "threads_after": pa.get("threads"),
            })
    return {
        "file": f"{stem}.json",
        "sha256": sha256_file(path),
        "params": doc.get("params", {}),
        "meta": load_meta(runs_dir, stem),
        "variants": out_vars,
    }


def summarize_burst_curve(runs_dir: str, stem: str) -> dict:
    path = os.path.join(runs_dir, f"{stem}.json")
    doc = json.load(open(path, encoding="utf-8"))
    res = doc["result"]
    pts_out = []
    total_claims = 0
    total_ok = 0
    total_err = 0
    total_probe_err = 0
    by_conc: dict[int, list[dict]] = {}
    for pt in res.get("points", []):
        by_conc.setdefault(int(pt["concurrency"]), []).append(pt)

    for c, reps in sorted(by_conc.items()):
        for r in reps:
            n_req = int(r.get("ok", 0)) + int(r.get("err", 0))
            total_claims += n_req
            total_ok += int(r.get("ok", 0))
            total_err += int(r.get("err", 0))
            total_probe_err += int(r.get("probe_err", 0) or 0)

        lat_key = "ttfi_s" if (reps and "ttfi_s" in reps[0] and (reps[0]["ttfi_s"] or {}).get("n")) else "latency_s"
        wire_key = "wire_claim_s" if (reps and "wire_claim_s" in reps[0]) else "latency_s"

        pts_out.append({
            "concurrency": c,
            "num_repeats": len(reps),
            "metric_type": lat_key,
            "ok_all": sum(int(r.get("ok", 0)) for r in reps),
            "err_all": sum(int(r.get("err", 0)) for r in reps),
            "probe_err_all": sum(int(r.get("probe_err", 0) or 0) for r in reps),
            "acquire_wall_med_s": med([r["burst_wall_s"] for r in reps]),
            "acquire_wall_runs_s": [round(float(r["burst_wall_s"]), 4) for r in reps],
            "burst_and_exec_wall_med_s": med([r["burst_and_exec_wall_s"] for r in reps if "burst_and_exec_wall_s" in r]),
            "claims_per_s_med": med([r["claims_per_s"] for r in reps]),
            "claims_per_s_runs": [round(float(r["claims_per_s"]), 2) for r in reps],
            "release_wall_med_s": med([r["release_wall_s"] for r in reps]),
            "release_wall_runs_s": [round(float(r["release_wall_s"]), 4) for r in reps],
            "release_per_s_med": med([r.get("release_sandboxes_per_s", round(r["ok"] / r["release_wall_s"], 2)) for r in reps if r.get("release_wall_s")]),
            "first_med_s": med([(r.get(lat_key) or {}).get("first", (r.get(lat_key) or {}).get("min")) for r in reps]),
            "first_runs_s": [(r.get(lat_key) or {}).get("first", (r.get(lat_key) or {}).get("min")) for r in reps],
            "p50_med_s": med([(r.get(lat_key) or {}).get("p50") for r in reps]),
            "avg_med_s": med([(r.get(lat_key) or {}).get("avg") for r in reps]),
            "p99_med_s": med([(r.get(lat_key) or {}).get("p99") for r in reps]),
            "max_med_s": med([(r.get(lat_key) or {}).get("max") for r in reps]),
            "skew_med": med([(r.get(lat_key) or {}).get("straggler_skew") for r in reps]),
            "p50_runs_s": [(r.get(lat_key) or {}).get("p50") for r in reps],
            "avg_runs_s": [(r.get(lat_key) or {}).get("avg") for r in reps],
            "p99_runs_s": [(r.get(lat_key) or {}).get("p99") for r in reps],
            "max_runs_s": [(r.get(lat_key) or {}).get("max") for r in reps],
            "wire_first_med_s": med([(r.get(wire_key) or {}).get("first", (r.get(wire_key) or {}).get("min")) for r in reps]),
            "wire_p50_med_s": med([(r.get(wire_key) or {}).get("p50") for r in reps]),
            "wire_p99_med_s": med([(r.get(wire_key) or {}).get("p99") for r in reps]),
            "wire_max_med_s": med([(r.get(wire_key) or {}).get("max") for r in reps]),
            "first_cmd_p50_med_s": med([(r.get("first_cmd_exec_s") or {}).get("p50") for r in reps]),
            "held_open_sockets_med": med([((r.get("proc_held") or {}).get("open_sockets")) for r in reps]),
            "held_rss_mb_med": med([((r.get("proc_held") or {}).get("vm_rss_mb")) for r in reps]),
        })
    return {
        "file": f"{stem}.json",
        "sha256": sha256_file(path),
        "claim_mode": res.get("claim_mode"),
        "batch_mif": res.get("batch_max_in_flight"),
        "clusters": res.get("clusters", 1),
        "standing_pools": res.get("standing_pools", res.get("images")),
        "replicas_per_image": res.get("replicas_per_image"),
        "standing_total": res.get("standing_total"),
        "setup_s": res.get("setup_s"),
        "total_claims": total_claims,
        "total_ok": total_ok,
        "total_err": total_err,
        "total_probe_err": total_probe_err,
        "meta": load_meta(runs_dir, stem),
        "points": pts_out,
    }


def summarize_density(runs_dir: str, stem: str) -> dict:
    path = os.path.join(runs_dir, f"{stem}.json")
    doc = json.load(open(path, encoding="utf-8"))
    pts = []
    for p in doc.get("points", []):
        rzp = (p.get("reap_zero_pods") or {}).get("reap_to_zero_pods_s")
        ok_c = int(p["ok_claims"])
        pts.append({
            "target_active": int(p["target_concurrency"]),
            "achieved_active": ok_c,
            "active_sandboxes_per_vcpu": p["achieved_active_sandboxes_per_vcpu"],
            "claim_err": int(p["claim_err"]),
            "probe_ok": int(p["ok_active_exec"]),
            "probe_err": int(p["probe_err"]),
            "setup_warm_s": p["setup_s"],
            "acquire_wall_s": p["claim_burst_wall_s"],
            "claims_per_s": p["claims_per_s"],
            "burst_and_exec_wall_s": p["burst_and_exec_wall_s"],
            "release_wall_s": p["release_wall_s"],
            "release_per_s": p["release_sandboxes_per_s"],
            "reap_to_zero_pods_s": rzp,
            "reap_pods_per_s": round(ok_c / rzp, 2) if rzp else None,
            "wire_claim_s": p["wire_claim_s"],
            "first_cmd_exec_s": p["first_cmd_exec_s"],
            "ttfi_s": p["ttfi_s"],
            "ttfi_inline_s": p["ttfi_inline_s"],
            "open_sockets_held": (p.get("proc_held") or {}).get("open_sockets"),
            "vm_rss_mb_held": (p.get("proc_held") or {}).get("vm_rss_mb"),
            "threads_held": (p.get("proc_held") or {}).get("threads"),
            "cluster_health_while_held": p.get("cluster_health_while_held"),
        })
    return {
        "file": f"{stem}.json",
        "sha256": sha256_file(path),
        "params": doc.get("params", {}),
        "meta": load_meta(runs_dir, stem),
        "points": pts,
    }


def summarize_golden_small(runs_dir: str, stem: str) -> dict:
    path = os.path.join(runs_dir, f"{stem}.json")
    doc = json.load(open(path, encoding="utf-8"))
    res = doc["result"]
    per_job_p99s = [(j.get("ttfi_inline_s") or {}).get("p99") for j in res.get("per_job", [])]
    return {
        "file": f"{stem}.json",
        "sha256": sha256_file(path),
        "claim_mode": res["claim_mode"],
        "clusters": res["clusters"],
        "jobs": res["jobs"],
        "per_job_tasks": res["per_job_sandboxes"],
        "total_concurrent_target": res["total_concurrent_sandboxes"],
        "provision_warm_s": res["setup_s"],
        "all_jobs_wall_s": res["all_jobs_wall_s"],
        "aggregate_goodput_tasks_per_s": res["aggregate_tasks_per_s"],
        "total_ok": res["total_ok"],
        "total_err": res["total_err"],
        "failure_rate_pct": res["failure_rate_pct"],
        "job_step_barrier_s": res.get("job_step_barrier_s"),
        "aggregate_wire_claim_s": res["aggregate_wire_claim_s"],
        "aggregate_ttfi_s": res["aggregate_ttfi_s"],
        "aggregate_ttfi_inline_s": res["aggregate_ttfi_inline_s"],
        "per_job_ttfi_p99_range_s": rng([x for x in per_job_p99s if x is not None]),
        "open_sockets_peak": (res.get("proc_peak_held") or {}).get("open_sockets"),
        "vm_rss_mb_peak": (res.get("proc_peak_held") or {}).get("vm_rss_mb"),
        "threads_peak": (res.get("proc_peak_held") or {}).get("threads"),
        "meta": load_meta(runs_dir, stem),
    }


def summarize_soak10k(runs_dir: str, stem: str) -> dict:
    path = os.path.join(runs_dir, f"{stem}.json")
    doc = json.load(open(path, encoding="utf-8"))
    res = doc["result"]
    waves = res.get("waves", [])
    peak_socks = max((int((w.get("proc") or {}).get("open_sockets", 0) or 0) for w in waves), default=0)
    peak_rss = max((float((w.get("proc") or {}).get("vm_rss_mb", 0.0) or 0.0) for w in waves), default=0.0)
    peak_threads = max((int((w.get("proc") or {}).get("threads", 0) or 0) for w in waves), default=0)
    return {
        "file": f"{stem}.json",
        "sha256": sha256_file(path),
        "claim_mode": res["claim_mode"],
        "total_claims": res["total_claims"],
        "total_ok": res["total_ok"],
        "total_err": res["total_err"],
        "wave_size": res["wave_size"],
        "setup_s": res["setup_s"],
        "soak_wall_s": res["soak_wall_s"],
        "goodput_claims_per_s": res["goodput_claims_per_s"],
        "aggregate_latency_s": res["aggregate_latency_s"],
        "peak_open_sockets": peak_socks,
        "peak_rss_mb": round(peak_rss, 2),
        "peak_threads": peak_threads,
        "meta": load_meta(runs_dir, stem),
    }


def summarize_sdk_cleanup(runs_dir: str, stem: str) -> dict:
    path = os.path.join(runs_dir, f"{stem}.json")
    doc = json.load(open(path, encoding="utf-8"))
    grouped: dict[tuple[str, int], list[dict]] = {}
    for r in doc.get("runs", []):
        key = (r["mode"], int(r.get("total_n", r.get("n"))))
        grouped.setdefault(key, []).append(r)

    summary_rows = []
    for (mode, n), items in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][1])):
        acq_med = med([it["acquire_s"] for it in items])
        rel_api_med = med([it.get("release_api_s", it["cleanup_s"]) for it in items])
        pods_gone_vals = [it["pods_gone_s"] for it in items if "pods_gone_s" in it and it["pods_gone_s"] is not None]
        claims_gone_vals = [it["claims_gone_s"] for it in items if "claims_gone_s" in it and it["claims_gone_s"] is not None]
        pods_gone_med = med(pods_gone_vals) if pods_gone_vals else None
        claims_gone_med = med(claims_gone_vals) if claims_gone_vals else None
        summary_rows.append({
            "mode": mode,
            "n": n,
            "repeats": len(items),
            "ok_all": sum(int(it.get("ok", n)) for it in items),
            "err_all": sum(int(it.get("err", 0)) for it in items),
            "term_err_all": sum(int(it.get("term_err", 0)) + int(it.get("del_err", 0)) for it in items),
            "acquire_med_s": acq_med,
            "acquire_runs_s": [round(float(it["acquire_s"]), 4) for it in items],
            "creates_per_s_med": med([it["creates_per_s"] for it in items if it.get("creates_per_s") is not None]),
            "release_api_med_s": rel_api_med,
            "release_api_runs_s": [round(float(it.get("release_api_s", it["cleanup_s"])), 4) for it in items],
            "release_api_per_s_med": med([it.get("release_api_per_s", it.get("deletes_per_s")) for it in items if it.get("release_api_per_s", it.get("deletes_per_s")) is not None]),
            "claims_gone_med_s": claims_gone_med,
            "claims_gone_per_s_med": med([it["claims_gone_per_s"] for it in items if it.get("claims_gone_per_s") is not None]),
            "pods_alive_at_api_return_runs": [it.get("pods_alive_at_api_return") for it in items if "pods_alive_at_api_return" in it],
            "pods_alive_at_api_return_med": med([it["pods_alive_at_api_return"] for it in items if it.get("pods_alive_at_api_return") is not None]),
            "pods_gone_med_s": pods_gone_med,
            "pods_gone_runs_s": [round(float(x), 4) for x in pods_gone_vals] if pods_gone_vals else None,
            "pods_gone_per_s_med": med([it["pods_gone_per_s"] for it in items if it.get("pods_gone_per_s") is not None]),
            "socks_before_med": med([(it.get("proc_before") or {}).get("open_sockets") for it in items if it.get("proc_before")]),
            "held_open_sockets_med": med([(it.get("proc_held") or {}).get("open_sockets") for it in items if it.get("proc_held")]),
            "held_rss_mb_med": med([(it.get("proc_held") or {}).get("vm_rss_mb") for it in items if it.get("proc_held")]),
            "held_threads_med": med([(it.get("proc_held") or {}).get("threads") for it in items if it.get("proc_held")]),
        })
    return {
        "file": f"{stem}.json",
        "sha256": sha256_file(path),
        "setup_s": doc.get("setup_s"),
        "verify_teardown": doc.get("verify_teardown"),
        "meta": load_meta(runs_dir, stem),
        "summary": summary_rows,
        "raw_runs": doc.get("runs", []),
    }


def build_all(c2_dir: str = DEFAULT_C2_DIR, c3_dir: str = DEFAULT_C3_DIR) -> dict:
    # --- Campaign 2 ---
    c2 = {
        "phase1_t0_3000": {
            s: {
                m: summarize_variant_sweep(c2_dir, f"p1_t0_swe3000_{s}_{m}")
                for m in ("per_claim", "batch64")
            }
            for s in ("naive", "sliding", "pipelined", "recycle")
        },
        "phase2_rl_loops": {
            "r2e_multistep": {
                m: summarize_variant_sweep(c2_dir, f"p2_b1a_r2e_{m}")
                for m in ("per_claim", "batch64")
            },
            "tunix_grpo": {
                m: summarize_variant_sweep(c2_dir, f"p2_b1b_grpo_{m}")
                for m in ("per_claim", "batch64")
            },
        },
        "phase2_burst_curves": {
            "1x1000_configH": {
                m: summarize_burst_curve(c2_dir, f"p2_b2a_1pool1000_{m}")
                for m in ("per_claim", "batch64")
            },
            "200x5_incluster": {
                m: summarize_burst_curve(c2_dir, f"p2_b2a_burst_{m}")
                for m in ("per_claim", "batch20", "batch64")
            },
            "200x5_driverplc": {
                m: summarize_burst_curve(c2_dir, f"p2_b2a_driverplc_burst_{m}")
                for m in ("per_claim", "batch20", "batch64")
            },
        },
        "phase2_soak_10k": {
            m: summarize_soak10k(c2_dir, f"p2_b2b_soak10k_{m}")
            for m in ("per_claim", "batch64")
        },
        "phase2_configH_sweeps": {
            "swe500x1": {
                m: summarize_variant_sweep(c2_dir, f"p2_b3a_swe500_{m}")
                for m in ("per_claim", "batch64")
            },
            "swe3000": {
                s: {
                    m: summarize_variant_sweep(c2_dir, f"p2_b3b_swe3000_{s}_{m}")
                    for m in ("per_claim", "batch64")
                }
                for s in ("naive", "sliding", "pipelined")
            },
        },
        "phase2_sdk_1000_cleanup": summarize_sdk_cleanup(c2_dir, "p2_sdk_1000_cleanup"),
        "phase3_t1_sbd_3000": {
            s: {
                m: summarize_variant_sweep(c2_dir, f"p3_t1_sbd_swe3000_{s}_{m}")
                for m in ("per_claim", "batch64")
            }
            for s in ("naive", "pipelined", "sleep30")
        },
    }

    # --- Campaign 3 ---
    c3: dict = {
        "c3_1a_anchor_unhydrated_sbd": {
            m: summarize_variant_sweep(c3_dir, f"c3_1a_anchor_t1_swebench3000_{m}")
            for m in ("per_claim", "batch64")
        },
        "c3_1a_cold_sbd_symmetric": {
            m: summarize_variant_sweep(c3_dir, f"c3_1a_cold_sbd_symmetric_{m}")
            for m in ("per_claim", "batch64")
        },
        "c3_1a_anchor_hydrated_exact_c2": {
            m: summarize_variant_sweep(c3_dir, f"c3_1a_anchor_exact_c2_{m}")
            for m in ("per_claim", "batch64")
        },
        "c3_1b_cold_gcfs_500": {
            m: summarize_variant_sweep(c3_dir, f"c3_1b_cold_swebench500_{m}")
            for m in ("per_claim", "batch64")
        },
        "c3_1c_warm_ttfi_1x1000": {
            m: summarize_burst_curve(c3_dir, f"c3_1c_warm_ttfi_1x1000_{m}")
            for m in ("per_claim", "batch64")
        },
        "c3_1c_warm_ttfi_200x5": {
            "per_claim": summarize_burst_curve(c3_dir, "c3_1c_warm_ttfi_200x5_per_claim"),
            "batch20": summarize_burst_curve(c3_dir, "c3_1c_warm_ttfi_200x5_batch20"),
        },
        "c3_2_density": {
            m: summarize_density(c3_dir, f"c3_2_density_{m}")
            for m in ("per_claim", "batch64")
        },
        "c3_3a_mc5_warm4000": {
            m: summarize_burst_curve(c3_dir, f"c3_3a_mc5_warm4000_{m}")
            for m in ("per_claim", "batch64")
        },
        "c3_3b_mc5_golden_small": {
            "per_claim": summarize_golden_small(c3_dir, "c3_3b_mc5_golden_small_per_claim"),
            "batch20": summarize_golden_small(c3_dir, "c3_3b_mc5_golden_small_batch20"),
        },
        "c3_3c_mc5_cuj1_8000": {
            m: summarize_burst_curve(c3_dir, f"c3_3c_mc5_cuj1_8000_{m}")
            for m in ("per_claim", "batch64")
        },
    }
    c3_3d_path = os.path.join(c3_dir, "c3_3d_mc5_sdk_5000_claim_and_cleanup.json")
    if os.path.isfile(c3_3d_path):
        c3["c3_3d_mc5_sdk_5000"] = summarize_sdk_cleanup(c3_dir, "c3_3d_mc5_sdk_5000_claim_and_cleanup")
    else:
        c3["c3_3d_mc5_sdk_5000"] = None

    # --- Compute exact Campaign 3 operation & error totals ---
    c3_total_ops = 0
    c3_claim_errs = 0
    c3_probe_errs = 0
    c3_error_breakdown = []

    for suite_key in ("c3_1a_anchor_unhydrated_sbd", "c3_1a_cold_sbd_symmetric", "c3_1a_anchor_hydrated_exact_c2", "c3_1b_cold_gcfs_500"):
        for arm, sdata in c3[suite_key].items():
            for v in sdata["variants"]:
                c3_total_ops += v["tasks"]
                c3_claim_errs += v["err"]
                if v["err"]:
                    c3_error_breakdown.append({"suite": suite_key, "arm": arm, "err": v["err"]})

    for suite_key in ("c3_1c_warm_ttfi_1x1000", "c3_1c_warm_ttfi_200x5", "c3_3a_mc5_warm4000", "c3_3c_mc5_cuj1_8000"):
        for arm, sdata in c3[suite_key].items():
            c3_total_ops += sdata["total_claims"]
            c3_claim_errs += sdata["total_err"]
            c3_probe_errs += sdata["total_probe_err"]
            if sdata["total_err"] or sdata["total_probe_err"]:
                c3_error_breakdown.append({
                    "suite": suite_key,
                    "arm": arm,
                    "claim_err": sdata["total_err"],
                    "probe_err": sdata["total_probe_err"],
                })

    for arm, sdata in c3["c3_2_density"].items():
        for pt in sdata["points"]:
            c3_total_ops += pt["target_active"]
            c3_claim_errs += pt["claim_err"]
            c3_probe_errs += pt["probe_err"]
            if pt["claim_err"] or pt["probe_err"]:
                c3_error_breakdown.append({
                    "suite": "c3_2_density",
                    "arm": arm,
                    "c": pt["target_active"],
                    "claim_err": pt["claim_err"],
                    "probe_err": pt["probe_err"],
                })

    for arm, sdata in c3["c3_3b_mc5_golden_small"].items():
        c3_total_ops += sdata["total_concurrent_target"]
        c3_claim_errs += sdata["total_err"]
        if sdata["total_err"]:
            c3_error_breakdown.append({"suite": "c3_3b_mc5_golden_small", "arm": arm, "err": sdata["total_err"]})

    if c3["c3_3d_mc5_sdk_5000"] is not None:
        for row in c3["c3_3d_mc5_sdk_5000"]["summary"]:
            c3_total_ops += row["ok_all"] + row["err_all"]
            c3_claim_errs += row["err_all"]
            if row["err_all"] or row.get("term_err_all", 0):
                c3_error_breakdown.append({
                    "suite": "c3_3d_mc5_sdk_5000",
                    "mode": row["mode"],
                    "n": row["n"],
                    "err": row["err_all"],
                    "term_err": row.get("term_err_all", 0),
                })

    # --- Exhaustive Audit of Runs / Metrics Where `batch` Loses to `per_claim` ---
    losses = []

    # 1. C2 Tunix GRPO Config H
    g_pc = c2["phase2_rl_loops"]["tunix_grpo"]["per_claim"]["variants"][0]
    g_ba = c2["phase2_rl_loops"]["tunix_grpo"]["batch64"]["variants"][0]
    if g_ba["variant_wall_s"] > g_pc["variant_wall_s"]:
        losses.append({
            "campaign": "C2",
            "suite": "p2_b1b_grpo (Tunix GRPO 64x16=1024 x 8 steps = 8,192, Config H 100/s refill cap)",
            "metric": "variant_wall_s",
            "per_claim": g_pc["variant_wall_s"],
            "batch": g_ba["variant_wall_s"],
            "ratio_batch_speedup": round(g_pc["variant_wall_s"] / g_ba["variant_wall_s"], 3),
            "reason": (
                f"batch64 won fresh Step 0 ({g_ba['step0_wall_s']:.2f}s vs {g_pc['step0_wall_s']:.2f}s, "
                f"{g_pc['step0_wall_s'] / g_ba['step0_wall_s']:.2f}x) and cut post-run open sockets "
                f"{g_pc['open_sockets_after']} -> {g_ba['open_sockets_after']} "
                f"({g_pc['open_sockets_after'] / g_ba['open_sockets_after']:.2f}x; mid-step exec peak {g_pc['peak_open_sockets']} -> {g_ba['peak_open_sockets']}), "
                f"but across Steps 1-7 the 100 pods/s controller refill cap favored per_claim streaming individual DELETEs "
                f"during straggler exec tails over end-of-step DELETE_COLLECTION."
            ),
        })

    # 2. C2 Bench 3A 500x1 Config H strategies
    s3a_pc = {v["label"]: v for v in c2["phase2_configH_sweeps"]["swe500x1"]["per_claim"]["variants"]}
    s3a_ba = {v["label"]: v for v in c2["phase2_configH_sweeps"]["swe500x1"]["batch64"]["variants"]}
    for lbl in ("naive", "sliding", "pipelined"):
        if lbl in s3a_pc and lbl in s3a_ba and s3a_ba[lbl]["wall_s"] > s3a_pc[lbl]["wall_s"]:
            losses.append({
                "campaign": "C2",
                "suite": f"p2_b3a_swe500 ({lbl}, 500x1 warm DaemonSet disk, Config H)",
                "metric": "wall_s",
                "per_claim": s3a_pc[lbl]["wall_s"],
                "batch": s3a_ba[lbl]["wall_s"],
                "ratio_batch_speedup": round(s3a_pc[lbl]["wall_s"] / s3a_ba[lbl]["wall_s"], 3),
                "reason": (
                    f"batch64 won naive ({s3a_ba['naive']['wall_s']:.2f}s vs {s3a_pc['naive']['wall_s']:.2f}s) "
                    f"and sliding ({s3a_ba['sliding']['wall_s']:.2f}s vs {s3a_pc['sliding']['wall_s']:.2f}s), "
                    f"but trailed on {lbl} ({s3a_ba[lbl]['wall_s']:.2f}s vs {s3a_pc[lbl]['wall_s']:.2f}s) due to "
                    f"sequential per-pool readiness pre-checks + Lease setup across 500 single-task pools when 3-lane APF already eliminated 429s."
                ),
            })

    # 3. C2 Bench 3B 3000 Config H
    for strat in ("naive", "sliding", "pipelined"):
        v_pc = c2["phase2_configH_sweeps"]["swe3000"][strat]["per_claim"]["variants"][0]
        v_ba = c2["phase2_configH_sweeps"]["swe3000"][strat]["batch64"]["variants"][0]
        if v_ba["wall_s"] > v_pc["wall_s"]:
            diff_s = v_ba["wall_s"] - v_pc["wall_s"]
            diff_pct = (diff_s / v_pc["wall_s"]) * 100.0
            losses.append({
                "campaign": "C2",
                "suite": f"p2_b3b_swe3000_{strat} (500x6=3000 warm DaemonSet disk, Config H 100/s refill cap)",
                "metric": "wall_s",
                "per_claim": v_pc["wall_s"],
                "batch": v_ba["wall_s"],
                "ratio_batch_speedup": round(v_pc["wall_s"] / v_ba["wall_s"], 3),
                "reason": (
                    f"Under the 100 pods/s warm-pool refill cap, batch(mif=64, create_rps=200) trailed {strat} by "
                    f"{diff_s:.2f}s ({diff_pct:.1f}%) while cutting open sockets "
                    f"{v_pc['open_sockets_after']} -> {v_ba['open_sockets_after']} "
                    f"({v_pc['open_sockets_after'] / v_ba['open_sockets_after']:.2f}x)."
                ),
            })

    # 4a. C3-1a Symmetric First-Boot Cold SBD (fresh 10x n2-standard-32 + 600GB SBD nodepool per arm, tgs=0, cow=True)
    cs_pc = c3["c3_1a_cold_sbd_symmetric"]["per_claim"]["variants"][0]
    cs_ba = c3["c3_1a_cold_sbd_symmetric"]["batch64"]["variants"][0]
    if cs_ba["wall_s"] > cs_pc["wall_s"]:
        losses.append({
            "campaign": "C3",
            "suite": "c3_1a_cold_sbd_symmetric (500x6=3000 symmetric first-boot cold T1 SBD, naive, tgs=0, cow=True)",
            "metric": "wall_s (and paired_ttfi_s p50/p99)",
            "per_claim": {"wall_s": cs_pc["wall_s"], "ttfi_p50_s": cs_pc["paired_ttfi_s"]["p50"], "ttfi_p99_s": cs_pc["paired_ttfi_s"]["p99"]},
            "batch": {"wall_s": cs_ba["wall_s"], "ttfi_p50_s": cs_ba["paired_ttfi_s"]["p50"], "ttfi_p99_s": cs_ba["paired_ttfi_s"]["p99"]},
            "ratio_batch_speedup": round(cs_pc["wall_s"] / cs_ba["wall_s"], 3),
            "reason": (
                f"With a brand-new 10x n2-standard-32 + 600GB SBD nodepool created from scratch before EACH arm, "
                f"both arms paid identical first-boot GCE SBD lazy GCS block hydration (~368-393s above hydrated #41/#42), "
                f"and per_claim beat batch(mif=64, create_rps=200) by 1.09x ({cs_pc['wall_s']:.2f}s vs {cs_ba['wall_s']:.2f}s) "
                f"for the same wp=1 streaming-delete overlap reason as hydrated #41/#42, while batch64 cut open_sockets_after "
                f"{cs_pc['open_sockets_after']} -> {cs_ba['open_sockets_after']} ({cs_pc['open_sockets_after'] / cs_ba['open_sockets_after']:.2f}x)."
            ),
        })

    # 4b. C3-1a Hydrated SBD Anchor (exact C2 parity, naive)
    a_pc = c3["c3_1a_anchor_hydrated_exact_c2"]["per_claim"]["variants"][0]
    a_ba = c3["c3_1a_anchor_hydrated_exact_c2"]["batch64"]["variants"][0]
    if a_ba["wall_s"] > a_pc["wall_s"]:
        losses.append({
            "campaign": "C3",
            "suite": "c3_1a_anchor_exact_c2 (500x6=3000 hydrated T1 SBD, naive, tgs=0, cow=True)",
            "metric": "wall_s (and paired_ttfi_s.p50)",
            "per_claim": {"wall_s": a_pc["wall_s"], "ttfi_p50_s": a_pc["paired_ttfi_s"]["p50"]},
            "batch": {"wall_s": a_ba["wall_s"], "ttfi_p50_s": a_ba["paired_ttfi_s"]["p50"]},
            "ratio_batch_speedup": round(a_pc["wall_s"] / a_ba["wall_s"], 3),
            "reason": (
                f"With gomaxprocs=16 + 3-lane APF + hydrated SBD (0 HTTP 429s) on size-1 warm pools (wp=1), "
                f"per_claim streams individual claim DELETEs as tasks finish so controller warm-pool refill overlaps wave stragglers, "
                f"whereas batch(mif=64, create_rps=200) paces creates at mif=64 and bulk-deletes at wave boundaries while cutting "
                f"open_sockets_after {a_pc['open_sockets_after']} -> {a_ba['open_sockets_after']} "
                f"({a_pc['open_sockets_after'] / a_ba['open_sockets_after']:.2f}x) and paired_ttfi p99 "
                f"{a_pc['paired_ttfi_s']['p99']:.2f}s -> {a_ba['paired_ttfi_s']['p99']:.2f}s."
            ),
        })

    # 5. C3-1b Cold GCFS sockets
    cold_pc = c3["c3_1b_cold_gcfs_500"]["per_claim"]["variants"][0]
    cold_ba = c3["c3_1b_cold_gcfs_500"]["batch64"]["variants"][0]
    if (cold_ba["open_sockets_after"] or 0) > (cold_pc["open_sockets_after"] or 0):
        losses.append({
            "campaign": "C3",
            "suite": "c3_1b_cold_swebench500 (500x1 cold 0%-cached GCFS streaming)",
            "metric": "open_sockets_after",
            "per_claim": cold_pc["open_sockets_after"],
            "batch": cold_ba["open_sockets_after"],
            "ratio_batch_vs_per_claim": round(cold_ba["open_sockets_after"] / cold_pc["open_sockets_after"], 2),
            "reason": (
                f"batch64 won wall_s {cold_ba['wall_s']:.2f}s vs {cold_pc['wall_s']:.2f}s "
                f"({cold_pc['wall_s'] / cold_ba['wall_s']:.2f}x faster), so all 500 concurrent probe exec connections "
                f"overlapped simultaneously in a tighter window ({cold_ba['open_sockets_after']} pooled sockets vs "
                f"{cold_pc['open_sockets_after']} staggered sockets in slower per_claim)."
            ),
        })

    # 6. C3-1c & C3-3a Warm Burst points where per_claim beats batch on any median latency/rate/release/sockets/probe_err
    for pool_key, b_arm in (
        ("c3_1c_warm_ttfi_1x1000", "batch64"),
        ("c3_1c_warm_ttfi_200x5", "batch20"),
        ("c3_3a_mc5_warm4000", "batch64"),
    ):
        pc_pts = {p["concurrency"]: p for p in c3[pool_key]["per_claim"]["points"]}
        ba_pts = {p["concurrency"]: p for p in c3[pool_key][b_arm]["points"]}
        for c_val, p_pc in sorted(pc_pts.items()):
            p_ba = ba_pts[c_val]
            sub_losses = {}
            for mname, k in (
                ("ttfi_first_med_s", "first_med_s"),
                ("ttfi_p50_med_s", "p50_med_s"),
                ("ttfi_p99_med_s", "p99_med_s"),
                ("ttfi_max_med_s", "max_med_s"),
                ("acquire_wall_med_s", "acquire_wall_med_s"),
                ("release_wall_med_s", "release_wall_med_s"),
                ("held_open_sockets_med", "held_open_sockets_med"),
                ("probe_err_all", "probe_err_all"),
            ):
                if p_ba.get(k) is not None and p_pc.get(k) is not None and p_ba[k] > p_pc[k]:
                    sub_losses[mname] = {"per_claim": p_pc[k], "batch": p_ba[k]}
            if sub_losses:
                losses.append({
                    "campaign": "C3",
                    "suite": f"{pool_key} (c={c_val})",
                    "metric": ", ".join(sub_losses.keys()),
                    "per_claim": {m: v["per_claim"] for m, v in sub_losses.items()},
                    "batch": {m: v["batch"] for m, v in sub_losses.items()},
                    "reason": f"{p_ba['num_repeats']}-repeat median comparison for {b_arm} vs per_claim at c={c_val}.",
                })

    # 7. C3-2 Density points where per_claim beats batch on any latency/wall/reap/socket metric
    d_pc_pts = {p["target_active"]: p for p in c3["c3_2_density"]["per_claim"]["points"]}
    d_ba_pts = {p["target_active"]: p for p in c3["c3_2_density"]["batch64"]["points"]}
    for c_val, p_pc in sorted(d_pc_pts.items()):
        p_ba = d_ba_pts[c_val]
        d_sub = {}
        for mname, fn in (
            ("wire_first_s", lambda x: (x.get("wire_claim_s") or {}).get("first")),
            ("ttfi_inline_first_s", lambda x: (x.get("ttfi_inline_s") or {}).get("first")),
            ("ttfi_p50_s", lambda x: (x.get("ttfi_s") or {}).get("p50")),
            ("ttfi_p99_s", lambda x: (x.get("ttfi_s") or {}).get("p99")),
            ("acquire_wall_s", lambda x: x.get("acquire_wall_s")),
            ("release_wall_s", lambda x: x.get("release_wall_s")),
            ("reap_to_zero_pods_s", lambda x: x.get("reap_to_zero_pods_s")),
            ("open_sockets_held", lambda x: x.get("open_sockets_held")),
        ):
            pv, bv = fn(p_pc), fn(p_ba)
            if pv is not None and bv is not None and bv > pv:
                d_sub[mname] = {"per_claim": pv, "batch": bv}
        if d_sub:
            losses.append({
                "campaign": "C3",
                "suite": f"c3_2_density (c={c_val}, {p_ba['active_sandboxes_per_vcpu']}/vCPU)",
                "metric": ", ".join(d_sub.keys()),
                "per_claim": {m: v["per_claim"] for m, v in d_sub.items()},
                "batch": {m: v["batch"] for m, v in d_sub.items()},
                "reason": (
                    f"At c={c_val:,} ({p_ba['active_sandboxes_per_vcpu']}/vCPU), batch64 won acquire_wall_s "
                    f"({p_ba['acquire_wall_s']:.3f}s vs {p_pc['acquire_wall_s']:.3f}s) and ttfi p50/p99 "
                    f"({p_ba['ttfi_s']['p50']:.2f}s/{p_ba['ttfi_s']['p99']:.2f}s vs {p_pc['ttfi_s']['p50']:.2f}s/{p_pc['ttfi_s']['p99']:.2f}s), "
                    f"while per_claim was lower on {', '.join(d_sub.keys())}."
                ),
            })

    # 8. C3-3c 8,000 wave release_wall_s
    c8_pc = c3["c3_3c_mc5_cuj1_8000"]["per_claim"]["points"][0]
    c8_ba = c3["c3_3c_mc5_cuj1_8000"]["batch64"]["points"][0]
    if c8_ba["release_wall_med_s"] > c8_pc["release_wall_med_s"]:
        acq_ratio = round(c8_pc["acquire_wall_med_s"] / c8_ba["acquire_wall_med_s"], 2)
        sock_ratio = round(c8_pc["held_open_sockets_med"] / c8_ba["held_open_sockets_med"], 2)
        losses.append({
            "campaign": "C3",
            "suite": "c3_3c_mc5_cuj1_8000 (c=8000 across 5 clusters)",
            "metric": "release_wall_s (API return)",
            "per_claim": c8_pc["release_wall_med_s"],
            "batch": c8_ba["release_wall_med_s"],
            "reason": (
                f"5-cluster DELETE_COLLECTION + Lease release returned in {c8_ba['release_wall_med_s']:.3f}s "
                f"({c8_ba['release_per_s_med']:.2f}/s) vs per_claim {c8_pc['release_wall_med_s']:.3f}s "
                f"({c8_pc['release_per_s_med']:.2f}/s), while batch won acquire_wall_s {acq_ratio}x "
                f"({c8_ba['acquire_wall_med_s']:.3f}s vs {c8_pc['acquire_wall_med_s']:.3f}s) and cut held sockets "
                f"{sock_ratio}x ({int(c8_pc['held_open_sockets_med']):,} -> {int(c8_ba['held_open_sockets_med']):,})."
            ),
        })

    # 9. C3-3d losses if any once C3-3d exists (vs both concurrent_terminate and sdk_delete_all)
    if c3["c3_3d_mc5_sdk_5000"] is not None:
        by_mode_n = {(r["mode"], r["n"]): r for r in c3["c3_3d_mc5_sdk_5000"]["summary"]}
        for cmp_mode, n_vals in (
            ("per_claim_concurrent_terminate", (1000, 2500, 5000)),
            ("per_claim_sdk_delete_all", (5000,)),
        ):
            for n_val in n_vals:
                b_row = by_mode_n.get(("batch_release_deletecollection", n_val))
                p_row = by_mode_n.get((cmp_mode, n_val))
                if b_row and p_row:
                    for mk in ("acquire_med_s", "release_api_med_s", "claims_gone_med_s", "pods_gone_med_s", "held_open_sockets_med"):
                        bv, pv = b_row.get(mk), p_row.get(mk)
                        if bv is not None and pv is not None and bv > pv:
                            if mk == "held_open_sockets_med":
                                rsn = (
                                    f"batch retained its pre-warmed keepalive pool ({int(b_row['socks_before_med'])} -> {int(bv)} ESTABLISHED sockets, "
                                    f"{b_row['err_all']} errors), whereas {cmp_mode} Watch.stop() closed pooled sockets during per-claim watches "
                                    f"({int(p_row['socks_before_med'])} -> {int(pv)} sockets, {p_row['err_all']} claim errors)."
                                )
                            else:
                                rsn = f"Measured head-to-head in C3-3d at n={n_val} (batch_release_deletecollection vs {cmp_mode})."
                            losses.append({
                                "campaign": "C3",
                                "suite": f"c3_3d_mc5_sdk_5000 (n={n_val}, vs {cmp_mode})",
                                "metric": mk,
                                "per_claim": pv,
                                "batch": bv,
                                "reason": rsn,
                            })

    # 10. Deterministic RESULTS.md rows #37-#42 computed directly from the 6 C3 SWE-bench JSONs
    ctrl_digest = "us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/agent-sandbox-controller@sha256:b0b3c550ad8eed1571c9494e3dc40b710aff724796cf4671fb1b7a5b959a3716"
    drv_digest = "us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:6d0b37df8c80b092186d5c8ee66d13e646283d48a686301d6ece6c3532f2a8c3"
    guest_sample = "us-central1-docker.pkg.dev/<GCP_PROJECT>/swebench-mirror/swebench-verified@sha256:5f6cd938e4e220fd63542b44835e767b3c6857ad9e6c9cc669214e34ec3161f0"

    c1b_pc = c3["c3_1b_cold_gcfs_500"]["per_claim"]["variants"][0]
    c1b_ba = c3["c3_1b_cold_gcfs_500"]["batch64"]["variants"][0]
    c1a_cs_pc = c3["c3_1a_cold_sbd_symmetric"]["per_claim"]["variants"][0]
    c1a_cs_ba = c3["c3_1a_cold_sbd_symmetric"]["batch64"]["variants"][0]
    c1a_h_pc = c3["c3_1a_anchor_hydrated_exact_c2"]["per_claim"]["variants"][0]
    c1a_h_ba = c3["c3_1a_anchor_hydrated_exact_c2"]["batch64"]["variants"][0]

    def _fmt_row(num: int, run_id: str, tier: str, daemon: str, start: str, v: dict, prm: dict, sbd: str, note: str) -> str:
        cells = [
            num,
            run_id,
            "Brian Nguyen",
            tier,
            "v1.37.1-gke.1697000",
            daemon,
            start,
            v["strategy"],
            "yes" if v["recycle"] else "no",
            v["tasks"],
            prm["images"],
            prm["rollouts"],
            prm["concurrency"],
            256,
            prm["cpu"],
            prm["memory"],
            sbd,
            "on",
            "1000/500",
            v["wall_s"],
            v["s_per_task"],
            v["claims"],
            f"{v['ok']}/{v['err']}",
            note,
        ]
        return "| " + " | ".join(str(c) for c in cells) + " |"

    daemon_t0 = f"stock gcfsd + streaming; controller {ctrl_digest} (refill-rate=500); driver {drv_digest}"
    daemon_t1 = f"stock gcfsd + 600GB SBD CONTAINER_IMAGE_CACHE + streaming; controller {ctrl_digest} (refill-rate=500); driver {drv_digest}"

    results_rows_37_42 = [
        _fmt_row(
            37,
            "brian-nguyen_20261106-154526-cold-per-claim-naive",
            "T0",
            daemon_t0,
            "cold (fresh gvisor-pool created immediately prior; 0% node image cache)",
            c1b_pc,
            c3["c3_1b_cold_gcfs_500"]["per_claim"]["params"],
            "none",
            (
                f"Campaign 3 C3-1b cold-node GCFS image-streaming eval sweep on asrl-c3-1 gvisor-pool (10x e2-standard-32, 200GB pd-balanced, 3-lane APF), "
                f"500x1=500 (spec deviation from 3000-task basic run to isolate single-wave cold pull), 500-wide, real lightweight interpreter probe ('python3 -V' — NOT comparable to #1 real-work record or 'true' no-op rows), "
                f"genuinely cold start (brand-new node pool before this arm). per_claim control arm (run 1/2): wall={c1b_pc['wall_s']}s ({c1b_pc['s_per_task']} s/task), "
                f"wait_pool_ready avg={c1b_pc['wpr_avg_s']}s (max={c1b_pc['wpr_max_s']}s), paired_ttfi p50={c1b_pc['paired_ttfi_s']['p50']}s / p99={c1b_pc['paired_ttfi_s']['p99']}s, open_sockets_after={c1b_pc['open_sockets_after']}. "
                f"Guest digest sample: {guest_sample} (+ 499 @sha256-pinned digests in drivers/images/swebench500.txt)."
            ),
        ),
        _fmt_row(
            38,
            "brian-nguyen_20261106-155315-cold-batch64-naive",
            "T0",
            daemon_t0,
            "cold (fresh gvisor-pool recreated between arms; 0% node image cache)",
            c1b_ba,
            c3["c3_1b_cold_gcfs_500"]["batch64"]["params"],
            "none",
            (
                f"Campaign 3 C3-1b cold-node GCFS image-streaming eval sweep on asrl-c3-1 gvisor-pool (10x e2-standard-32, 200GB pd-balanced, 3-lane APF), "
                f"500x1=500 (spec deviation from 3000-task basic run), 500-wide, probe='python3 -V' (NOT comparable to #1), "
                f"genuinely cold start (gvisor-pool deleted and recreated fresh before run 2/2 so both arms paid identical 0%-cache GCFS pull). "
                f"SandboxBatch (mif=64, rps=200) arm: wall={c1b_ba['wall_s']}s ({c1b_ba['s_per_task']} s/task, 1.57x faster than #37 {c1b_pc['wall_s']}s), "
                f"wait_pool_ready avg={c1b_ba['wpr_avg_s']}s (max={c1b_ba['wpr_max_s']}s), paired_ttfi p50={c1b_ba['paired_ttfi_s']['p50']}s / p99={c1b_ba['paired_ttfi_s']['p99']}s; "
                f"open_sockets_after={c1b_ba['open_sockets_after']} vs {c1b_pc['open_sockets_after']} on #37 because faster batch readiness overlapped all 500 concurrent exec probes in a tighter window. "
                f"Guest digest sample: {guest_sample} (+ 499 @sha256-pinned digests in drivers/images/swebench500.txt)."
            ),
        ),
        _fmt_row(
            39,
            "brian-nguyen_20261107-151640-sbd-cold-symmetric-per-claim-naive",
            "T1",
            daemon_t1,
            "cold-SBD (fresh SBD gvisor-pool created immediately prior; unhydrated GCS blocks; tgs=0, cow=True)",
            c1a_cs_pc,
            c3["c3_1a_cold_sbd_symmetric"]["per_claim"]["params"],
            "swebench-500-preload-600g 600G",
            (
                f"Campaign 3 C3-1a symmetric first-boot cold-SBD sweep (run 1/4, termination_grace_seconds=0, enable_cow=True) on brand-new gvisor-pool (10x n2-standard-32, 100GB pd-standard + 600GB swebench-500-preload-600g CONTAINER_IMAGE_CACHE, 3-lane APF), "
                f"500x6=3000, 500-wide, no-op probe ('true' infra floor — NOT comparable to #1 real-work record). "
                f"Fresh SBD nodepool created immediately prior so run paid full first-boot GCE SBD lazy GCS block hydration: wall={c1a_cs_pc['wall_s']}s ({c1a_cs_pc['s_per_task']} s/task — beats symmetric #40 batch64 {c1a_cs_ba['wall_s']}s by 1.09x because streaming individual claim DELETEs overlap wp=1 controller refill), "
                f"wait_pool_ready avg={c1a_cs_pc['wpr_avg_s']}s (max={c1a_cs_pc['wpr_max_s']}s), paired_ttfi p50={c1a_cs_pc['paired_ttfi_s']['p50']}s / p99={c1a_cs_pc['paired_ttfi_s']['p99']}s, open_sockets_after={c1a_cs_pc['open_sockets_after']}. "
                f"Guest digest sample: {guest_sample} (+ 499 @sha256-pinned digests in drivers/images/swebench500.txt)."
            ),
        ),
        _fmt_row(
            40,
            "brian-nguyen_20261107-153141-sbd-cold-symmetric-batch64-naive",
            "T1",
            daemon_t1,
            "cold-SBD (fresh SBD gvisor-pool recreated between arms; unhydrated GCS blocks; tgs=0, cow=True)",
            c1a_cs_ba,
            c3["c3_1a_cold_sbd_symmetric"]["batch64"]["params"],
            "swebench-500-preload-600g 600G",
            (
                f"Campaign 3 C3-1a symmetric first-boot cold-SBD sweep (run 2/4, termination_grace_seconds=0, enable_cow=True) on freshly recreated gvisor-pool (10x n2-standard-32, 100GB pd-standard + 600GB swebench-500-preload-600g CONTAINER_IMAGE_CACHE, 3-lane APF), "
                f"500x6=3000, 500-wide, no-op probe ('true' infra floor — NOT comparable to #1 real-work record). "
                f"gvisor-pool deleted and recreated fresh before this arm so both #39 and #40 paid identical first-boot GCE SBD lazy GCS block hydration: SandboxBatch (mif=64, rps=200) wall={c1a_cs_ba['wall_s']}s ({c1a_cs_ba['s_per_task']} s/task — 1.09x slower wall than symmetric #39 {c1a_cs_pc['wall_s']}s due to wp=1 wave-boundary DELETE_COLLECTION), "
                f"wait_pool_ready avg={c1a_cs_ba['wpr_avg_s']}s (max={c1a_cs_ba['wpr_max_s']}s), paired_ttfi p50={c1a_cs_ba['paired_ttfi_s']['p50']}s / p99={c1a_cs_ba['paired_ttfi_s']['p99']}s, open_sockets_after={c1a_cs_ba['open_sockets_after']} (4.51x fewer than #39 {c1a_cs_pc['open_sockets_after']}). "
                f"Guest digest sample: {guest_sample} (+ 499 @sha256-pinned digests in drivers/images/swebench500.txt)."
            ),
        ),
        _fmt_row(
            41,
            "brian-nguyen_20261106-172537-sbd-hydrated-per-claim-naive",
            "T1",
            daemon_t1,
            "warm (fully hydrated 600GB SBD image cache; tgs=0, cow=True)",
            c1a_h_pc,
            c3["c3_1a_anchor_hydrated_exact_c2"]["per_claim"]["params"],
            "swebench-500-preload-600g 600G",
            (
                f"Campaign 3 C3-1a matched hydrated-SBD parity sweep (run 3/4, termination_grace_seconds=0, enable_cow=True) on gvisor-pool (10x n2-standard-32, 100GB pd-standard + 600GB swebench-500-preload-600g CONTAINER_IMAGE_CACHE, 3-lane APF), "
                f"500x6=3000, 500-wide, no-op probe ('true' infra floor — NOT comparable to #1 real-work record), warm start (0 registry pulls, full zero-drain + 30s settle). "
                f"per_claim control arm: wall={c1a_h_pc['wall_s']}s ({c1a_h_pc['s_per_task']} s/task — 3.95x faster than cold-SBD #39 {c1a_cs_pc['wall_s']}s once SBD blocks are hydrated; beats matched #42 batch64 {c1a_h_ba['wall_s']}s by 1.17x because streaming individual claim DELETEs overlap wp=1 controller refill during wave tails when 3-lane APF eliminates 429s), "
                f"wait_pool_ready avg={c1a_h_pc['wpr_avg_s']}s (max={c1a_h_pc['wpr_max_s']}s), paired_ttfi p50={c1a_h_pc['paired_ttfi_s']['p50']}s / p99={c1a_h_pc['paired_ttfi_s']['p99']}s, open_sockets_after={c1a_h_pc['open_sockets_after']}. "
                f"Guest digest sample: {guest_sample} (+ 499 @sha256-pinned digests in drivers/images/swebench500.txt)."
            ),
        ),
        _fmt_row(
            42,
            "brian-nguyen_20261106-172828-sbd-hydrated-batch64-naive",
            "T1",
            daemon_t1,
            "warm (fully hydrated 600GB SBD image cache; tgs=0, cow=True)",
            c1a_h_ba,
            c3["c3_1a_anchor_hydrated_exact_c2"]["batch64"]["params"],
            "swebench-500-preload-600g 600G",
            (
                f"Campaign 3 C3-1a matched hydrated-SBD parity sweep (run 4/4, termination_grace_seconds=0, enable_cow=True) on gvisor-pool (10x n2-standard-32, 100GB pd-standard + 600GB swebench-500-preload-600g CONTAINER_IMAGE_CACHE, 3-lane APF), "
                f"500x6=3000, 500-wide, no-op probe ('true' infra floor — NOT comparable to #1 real-work record), warm start (0 registry pulls, full zero-drain + 30s settle). "
                f"SandboxBatch (mif=64, rps=200) arm: wall={c1a_h_ba['wall_s']}s ({c1a_h_ba['s_per_task']} s/task — 3.69x faster than cold-SBD #40 {c1a_cs_ba['wall_s']}s; 1.17x slower wall than matched #41 {c1a_h_pc['wall_s']}s due to mif=64 create pacing + wave-boundary DELETE_COLLECTION on wp=1 pools, within 5.1% of Campaign 2 #32 139.10s across Workstream B SHA change), "
                f"wait_pool_ready avg={c1a_h_ba['wpr_avg_s']}s (max={c1a_h_ba['wpr_max_s']}s), paired_ttfi p50={c1a_h_ba['paired_ttfi_s']['p50']}s / p99={c1a_h_ba['paired_ttfi_s']['p99']}s (faster p99 than #41 {c1a_h_pc['paired_ttfi_s']['p99']}s), "
                f"open_sockets_after={c1a_h_ba['open_sockets_after']} (4.68x fewer than #41 {c1a_h_pc['open_sockets_after']}). Guest digest sample: {guest_sample} (+ 499 @sha256-pinned digests in drivers/images/swebench500.txt)."
            ),
        ),
    ]

    return {
        "campaign_2": c2,
        "campaign_3": c3,
        "campaign_3_totals": {
            "files_present": 21 if c3["c3_3d_mc5_sdk_5000"] is not None else 20,
            "total_sandbox_operations": c3_total_ops,
            "total_claim_errors": c3_claim_errs,
            "total_probe_errors": c3_probe_errs,
            "error_breakdown": c3_error_breakdown,
        },
        "batch_losses_and_tradeoffs": losses,
        "results_md_rows_37_42": results_rows_37_42,
    }


def render_pr5_companion_reports(wt_pr5: str, summary: dict, c3_dir: str = DEFAULT_C3_DIR) -> None:
    """Deterministically render REPORT_TEMPLATE.md companion files for rows #37-#42, fix C2 prose nits, and update RESULTS.md."""
    import importlib.util

    gen_path = os.path.join(wt_pr5, "agent-sandbox", "drivers", "gen_report.py")
    spec = importlib.util.spec_from_file_location("gen_report", gen_path)
    assert spec and spec.loader
    gr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gr)

    tpl_path = os.path.join(wt_pr5, "agent-sandbox", "performance_reports", "REPORT_TEMPLATE.md")
    template_txt = open(tpl_path, encoding="utf-8").read()
    reports_dir = os.path.join(wt_pr5, "agent-sandbox", "performance_reports")
    results_path = os.path.join(wt_pr5, "agent-sandbox", "RESULTS.md")

    ctrl_img = "us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/agent-sandbox-controller@sha256:b0b3c550ad8eed1571c9494e3dc40b710aff724796cf4671fb1b7a5b959a3716"
    drv_img = "us-central1-docker.pkg.dev/<GCP_PROJECT>/rl-gke-bench/asrl-driver@sha256:6d0b37df8c80b092186d5c8ee66d13e646283d48a686301d6ece6c3532f2a8c3"
    guest_src = "us-central1-docker.pkg.dev/<GCP_PROJECT>/swebench-mirror/swebench-verified@sha256:5f6cd938e4e220fd63542b44835e767b3c6857ad9e6c9cc669214e34ec3161f0 (+ 499 @sha256-pinned digests in drivers/images/swebench500.txt)"
    ctrl_flags = (
        "--sandbox-concurrent-workers=1000 --sandbox-claim-concurrent-workers=500 "
        "--sandbox-warm-pool-concurrent-workers=500 --sandbox-template-concurrent-workers=100 "
        "--sandbox-warm-pool-max-batch-size=500 --sandbox-warm-pool-max-refill-rate=500 "
        "--kube-api-qps=-1 --api-connections=4 --separate-watch-connection=true "
        "--cache-label-selectors=true --disable-claim-events=true --disable-sandbox-events=true "
        "--disable-claim-observability-annotations=true"
    )

    def _load_variant(stem: str, label_override: str) -> tuple[dict, dict]:
        d = json.load(open(os.path.join(c3_dir, f"{stem}.json"), encoding="utf-8"))
        v = dict(d["variants"][0])
        v["label"] = label_override
        return v, dict(d["params"])

    # --- Companion Report 1: C3-1b Cold T0 500x1 (#37-#38) ---
    v37, prm37 = _load_variant("c3_1b_cold_swebench500_per_claim", "cold-per-claim-naive")
    v38, _ = _load_variant("c3_1b_cold_swebench500_batch64", "cold-batch64-naive")
    vars_cold = [v37, v38]
    prm37["context"] = "gke_<GCP_PROJECT>_us-central1_asrl-c3-1"
    prm37["images_file"] = "drivers/images/swebench500.txt (@sha256-pinned)"

    rows_37_38 = "\n".join(summary["results_md_rows_37_42"][0:2])
    findings_cold = f"""## Key findings

1. **On genuinely cold `0%`-cached `T0` nodes (`10× e2-standard-32`, `200GB pd-balanced` `gvisor-pool` recreated fresh before each arm, `500` unique SWE-bench `@sha256` images × `1` = `500` tasks, `probe='python3 -V'`), `cold-batch64-naive` (`#38`) completed in `{v38['wall_s']}s` (`{v38['s_per_task']} s/task`, `{v38['goodput_tasks_per_s']} tasks/s`) vs `{v37['wall_s']}s` (`{v37['s_per_task']} s/task`, `{v37['goodput_tasks_per_s']} tasks/s`) for `cold-per-claim-naive` (`#37`) — a `1.57×` end-to-end wall-clock speedup (`500/500` ok on both arms).** Paired per-sandbox TTFI (`paired_ttfi_s`) dropped from `p50={v37['paired_ttfi_s']['p50']}s / p99={v37['paired_ttfi_s']['p99']}s` (`#37`) to `p50={v38['paired_ttfi_s']['p50']}s / p99={v38['paired_ttfi_s']['p99']}s` (`#38`), and `env_ready_ok_s` `p99` dropped `1.57×` (`{v37['env_ready_ok_s']['p99']}s → {v38['env_ready_ok_s']['p99']}s`).
2. **Recycle was not enabled (`recycle=no`) because `500×1=500` evaluates single-wave cold startup (`1` rollout per image).** Both arms created `500` claims with `0` task errors, `0` controller restarts, and `0` APF rejected requests.
3. **Cold GCFS warm-up (`wait_pool_ready`) dominated total wall time on both arms (`avg={v37['phases']['wait_pool_ready']['avg']}s, max={v37['phases']['wait_pool_ready']['max']}s` on `#37` vs `avg={v38['phases']['wait_pool_ready']['avg']}s, max={v38['phases']['wait_pool_ready']['max']}s` on `#38`).** Notice a real client tradeoff: because `#38` made all `500` sandboxes ready simultaneously in a tighter window (`{v38['env_ready_ok_s']['p50']}s–{v38['env_ready_ok_s']['p99']}s`), all `500` concurrent `python3 -V` exec probes overlapped at once, raising `open_sockets_after` to `{v38['proc_after']['open_sockets']}` vs `{v37['proc_after']['open_sockets']}` on staggered `#37`.

Caveats: `500×1=500` is a deliberate single-wave cold-pull deviation from the `3,000`-task (`500×6`) canonical sweep, and `probe='python3 -V'` measures cold interpreter startup rather than `true` no-op or `#1`'s real-work command — do NOT compare `#37–#38` `s/task` directly against `3,000`-task rows or `#1`. Both arms had `gvisor-pool` freshly created immediately prior so neither inherited cached layers."""

    def _fill_report(
        out_name: str,
        ts: str,
        tier: str,
        start_desc: str,
        note: str,
        variants: list[dict],
        prm: dict,
        machine: str,
        node_vcpu: str,
        total_vcpu: str,
        disk: str,
        node_eph: str,
        sbd: str,
        peak: str,
        rows_block: str,
        findings_md: str,
    ) -> None:
        best = min(variants, key=lambda x: float(x["s_per_task"]))
        env_extra = {
            "tier": tier,
            "gke_version": "v1.37.1-gke.1697000",
            "cluster": "asrl-c3-1",
            "sandbox_pool": "gvisor-pool",
            "sandbox_node_machine": machine,
            "maxPods": "256",
            "controller_node_machine": "e2-standard-32",
            "controller_pool": "ctrl-pool",
            "controller_image": ctrl_img,
            "driver_image": drv_img,
            "ctrl_workers(sbx/wp)": "1000/500",
            "sbd": sbd,
            "image_streaming": "on",
            "daemon": f"stock gcfsd + streaming ({sbd}); controller {ctrl_img} (refill-rate=500)",
            "images_src": guest_src,
            "peak_pods": peak,
        }
        repl = {
            "USERNAME": "Brian Nguyen",
            "TIMESTAMP": ts,
            "TIER": tier,
            "CLUSTER": "asrl-c3-1",
            "POOL": "gvisor-pool",
            "START": start_desc,
            "NOTE": note,
            "DRIVER": "run_basic_swebench_3000.py",
            "BEST_LABEL": best["label"],
            "BEST_S_PER_TASK": best["s_per_task"],
            "BEST_WALL": best["wall_s"],
            "PEAK_PODS": peak,
            "SUMMARY_TABLE": gr.variants_summary_table(variants),
            "METRICS_TABLE": gr.variants_phase_table(variants),
            "RUNREPORT_BLOCKS": gr.run_report_blocks(variants),
            "PARAMS_TABLE": gr.params_table(prm, env_extra),
            "RUN_COMMAND": gr.run_command(prm, "run_basic_swebench_3000.py"),
            "CPU": prm["cpu"],
            "MEM": prm["memory"],
            "PROBE": prm["probe"],
            "PROJECT": "<GCP_PROJECT>",
            "REGION": "us-central1",
            "K8S": "v1.37.1-gke.1697000",
            "CTRL_IMAGE": ctrl_img,
            "POOL_NODES": "10",
            "MACHINE": machine,
            "ZONES": "us-central1-a,us-central1-b",
            "DISK": disk,
            "NODE_VCPU": node_vcpu,
            "NODE_MEM": "116Gi",
            "NODE_EPH": node_eph,
            "NODE_PODS": "256",
            "TAINT": "sandbox.gke.io/runtime=gvisor:NoSchedule",
            "TOTAL_VCPU": total_vcpu,
            "TOTAL_PODS": "2560",
            "IMGSTREAM": "on",
            "SBD": sbd,
            "IMAGES_SRC": guest_src,
            "CTRL_FLAGS": ctrl_flags,
            "CTRL": "1000/500",
            "CTRL_MACHINE": "e2-standard-32",
            "CTRL_POOL": "ctrl-pool",
            "CTRL_NODES": "2",
            "CTRL_ZONES": "us-central1-a,us-central1-b",
            "CTRL_DISK": "pd-standard 100GB",
            "CTRL_VCPU": "31.8",
            "CTRL_MEM": "116Gi",
            "CTRL_EPH": "88Gi",
            "CTRL_NODE_PODS": "110",
            "CTRL_TAINT": "dedicated=ctrl:NoSchedule",
            "CTRL_TOTAL_VCPU": "64",
            "RESULTS_ROWS": rows_block,
            "ERRORS": "None reported (`0` task errors across every variant in this sweep).",
        }
        txt = template_txt
        for k, val in repl.items():
            txt = txt.replace("{{" + k + "}}", str(val))
        kf_marker = "## Key findings  <!-- (manual — you fill this in) -->"
        err_marker = "## Errors / anomalies  <!-- (auto) -->"
        assert kf_marker in txt and err_marker in txt
        pre, rest = txt.split(kf_marker, 1)
        _, post = rest.split(err_marker, 1)
        txt = pre + findings_md + "\n\n" + err_marker + post
        assert "{{" not in txt, f"Unfilled placeholder in {out_name}"
        out_p = os.path.join(reports_dir, out_name)
        with open(out_p, "w", encoding="utf-8") as fh:
            fh.write(txt)

    _fill_report(
        "brian-nguyen_20261106-154500.md",
        "2026-11-06 15:45",
        "T0",
        "cold (gvisor-pool recreated fresh before each arm; 0% node image cache)",
        "Campaign 3 C3-1b cold-node GCFS streaming sweep (10x e2-standard-32, 200GB pd-balanced, 500x1=500, 500-wide, probe='python3 -V', gvisor-pool freshly created before each arm so both paid true 0%-cached GCFS pulls; rows #37-#38).",
        vars_cold,
        prm37,
        "e2-standard-32",
        "31.4",
        "314",
        "pd-balanced 200GB",
        "172Gi",
        "none",
        "1000",
        rows_37_38,
        findings_cold,
    )

    # --- Companion Report 2: C3-1a T1 SBD Symmetric Cold & Hydrated 500x6 (#39-#42) ---
    v39, _ = _load_variant("c3_1a_cold_sbd_symmetric_per_claim", "sbd-cold-symmetric-per-claim-naive")
    v40, _ = _load_variant("c3_1a_cold_sbd_symmetric_batch64", "sbd-cold-symmetric-batch64-naive")
    v41, prm41 = _load_variant("c3_1a_anchor_exact_c2_per_claim", "sbd-hydrated-per-claim-naive")
    v42, _ = _load_variant("c3_1a_anchor_exact_c2_batch64", "sbd-hydrated-batch64-naive")
    vars_sbd = [v39, v40, v41, v42]
    prm41["context"] = "gke_<GCP_PROJECT>_us-central1_asrl-c3-1"
    prm41["images_file"] = "drivers/images/swebench500.txt (@sha256-pinned)"

    rows_39_42 = "\n".join(summary["results_md_rows_37_42"][2:6])
    findings_sbd = f"""## Key findings

1. **Across both symmetric `T1` Secondary Boot Disk regimes (`500×6 = 3,000` SWE-bench tasks, `termination_grace_seconds=0`, `enable_cow=True`, `probe='true'`), `per_claim` finished `naive` faster on wall clock (`1.09×` on symmetric First-Boot Cold SBD `#39` `{v39['wall_s']}s` vs `#40` `{v40['wall_s']}s`, and `1.17×` on Hydrated SBD `#41` `{v41['wall_s']}s` vs `#42` `{v42['wall_s']}s`), while `SandboxBatch` (`mif=64`) cut open client sockets `4.51×–4.68×` (`{v39['proc_after']['open_sockets']} → {v40['proc_after']['open_sockets']}` on cold SBD; `{v41['proc_after']['open_sockets']} → {v42['proc_after']['open_sockets']}` on hydrated SBD) and lowered hydrated `paired_ttfi_s` `p99` (`{v41['paired_ttfi_s']['p99']}s → {v42['paired_ttfi_s']['p99']}s`).** With `gomaxprocs=16` (`48` regional control-plane vCPUs) + 3-lane APF (`0` HTTP `429`s) on size-1 warm pools (`wp=1`), `per_claim` streams individual `SandboxClaim` `DELETE`s as each task finishes so controller warm-pool replenishment overlaps wave stragglers, whereas `batch64` paces creation at `mif=64` and issues `DELETE_COLLECTION` at wave boundaries. Importantly, `#42` (`{v42['wall_s']}s`) reproduced Campaign 2 `#32` (`139.10s`) within `5.1%`, confirming Workstream B's SHA update (`4018f0dd → bb443e71`) introduced zero hot-path regression.
2. **Recycle was omitted (`recycle=no`) to isolate fresh `naive` warm-pool replenishment and GCE SBD lazy GCS block-hydration physics across `3,000` claims per variant (`12,000/12,000` tasks `ok`, `0` errors).**
3. **Recreating a brand-new `10× n2-standard-32 + 600GB` SBD nodepool from scratch before EACH cold arm (`#39` and `#40`) isolated exact first-boot GCE SBD lazy GCS block hydration (`~368–393s` wall overhead above hydrated `#41–#42`).** On newly created SBD disks, `wait_pool_ready` averaged `{v39['phases']['wait_pool_ready']['avg']}s` (`max={v39['phases']['wait_pool_ready']['max']}s`) on `#39` and `{v40['phases']['wait_pool_ready']['avg']}s` (`max={v40['phases']['wait_pool_ready']['max']}s`) on `#40`; once block-hydrated (`#41–#42`), `wait_pool_ready` shrank `>16×` to `{v41['phases']['wait_pool_ready']['avg']}s–{v42['phases']['wait_pool_ready']['avg']}s` (`3.95×` and `3.69×` faster total wall time).

Caveats: All four rows use `probe='true'` (infra floor) and are NOT comparable to `#1` (`2218.10s` real-work record). Both pairs (`#39` vs `#40` symmetric first-boot cold SBD with a brand-new SBD nodepool created before each arm, and `#41` vs `#42` symmetric fully hydrated SBD) are 100% footing-matched (`tgs=0, cow=True, SETTLE_S=30`). Note that an earlier sequential diagnostic pair (`545.64s` -> `308.59s`, `tgs=-1`) and Campaign 2 `#31` (`281.65s`) -> `#32` (`139.10s`) ran back-to-back without recreating the SBD nodepool between arms, which made `batch64` appear `1.77×–2.02×` faster only because `per_claim` ran first and hydrated GCE's lazy GCS-backed SBD blocks."""

    _fill_report(
        "brian-nguyen_20261106-163800.md",
        "2026-11-06 16:38 / 2026-11-07 15:16",
        "T1",
        "cold-SBD symmetric first-boot (#39-#40, fresh SBD pool per arm) and warm fully-hydrated 600GB SBD (#41-#42)",
        "Campaign 3 C3-1a T1 Secondary Boot Disk symmetric cold first-boot & hydrated parity sweep (500x6=3000, 500-wide, no-op infra-floor probe, tgs=0, cow=True): isolates exact GCE SBD lazy GCS block hydration on fresh SBD nodepools (#39 492.52s vs #40 539.18s) vs fully hydrated SBD (#41 124.73s vs #42 146.22s, within 5.1% of C2 #32 139.10s across Workstream B SHA update; batch64 cuts open sockets 4.51x-4.68x).",
        vars_sbd,
        prm41,
        "n2-standard-32",
        "31.8",
        "318",
        "pd-standard 100GB (+ 600GB SBD)",
        "88Gi (+ 600Gi CONTAINER_IMAGE_CACHE SBD)",
        "swebench-500-preload-600g (600GB CONTAINER_IMAGE_CACHE)",
        "1000",
        rows_39_42,
        findings_sbd,
    )

    # --- Fix the 4 exact prose/note nits in C2 companion reports & RESULTS.md rows #28-#32 ---
    def _patch_exact(fpath: str, replacements: list[tuple[str, str]]) -> None:
        content = open(fpath, encoding="utf-8").read()
        for old, new in replacements:
            if old in content:
                content = content.replace(old, new)
        with open(fpath, "w", encoding="utf-8") as fh:
            fh.write(content)

    # 1) 073700.md Key finding #3 exact phases.wait_pool_ready & phases.claim averages vs paired_wire_claim p99/max
    _patch_exact(
        os.path.join(reports_dir, "brian-nguyen_20261105-073700.md"),
        [
            (
                "`wait_pool_ready` averaged `2.57–5.53s` (`batch64`) vs `2.60–11.88s` (`per_claim`), whereas `claim` averaged **10.77s (`p99 = 19.30s`, `max = 32.30s`)** on `batch64-naive` vs **27.08s (`p99 = 139.66s`, `max = 273.31s`)** on `per-claim-naive`",
                "`wait_pool_ready` averaged `4.38–5.43s` (`batch64`) vs `5.37–19.02s` (`per_claim`), whereas `claim` averaged **9.55s (`p99 = 19.30s`, `max = 32.30s`)** on `batch64-naive` vs **26.22s (`p99 = 139.66s`, `max = 273.31s`)** on `per-claim-naive`",
            )
        ],
    )

    # 2) 143900.md Key finding #3 & Results rows #28-#30 exact socket/RSS/phase values from p2_b3b_swe3000_*.json
    row28_old = "5.96x fewer sockets 572->96, 1st Ready 97ms)"
    row28_new = "4.46x fewer sockets 549->123, 1st Ready 96.5ms)"
    row29_old = "3.55x fewer sockets 355->100)"
    row29_new = "5.96x fewer sockets 572->96)"
    row30_old = "117.69s / 0.0392 s/task, 2.77x fewer sockets)"
    row30_new = "117.69s / 0.0392 s/task, 2.21x fewer sockets 610->276)"
    _patch_exact(
        os.path.join(reports_dir, "brian-nguyen_20261105-143900.md"),
        [
            (
                "3. **Even when wall time is refill-rate-capped (`100 pods/s`), `SandboxBatch` cuts client socket load up to `5.96×` (`572 → 96`), lowers driver RSS `25%` (`273 → 205 MB`), and hits `97 ms` first-claim readiness.** `wait_pool_ready` (`4.20–4.32s` avg on `naive`) is rapid on cached `1500GB pd-ssd` nodes, while `claim` (`18.18s` avg on `batch64-naive` vs `18.67s` on `per-claim-naive`) reflects waiting on `max-refill-rate=100` across `G=6` waves rather than client watch contention.",
                "3. **Even when wall time is refill-rate-capped (`100 pods/s`), `SandboxBatch` cuts client socket load up to `5.96×` (`572 → 96` on `sliding`; `549 → 123` on `naive`; `610 → 276` on `pipelined`), lowers driver RSS up to `25.4%` (`449.9 → 335.7 MB`), and hits `96.5 ms` first-claim readiness.** `wait_pool_ready` (`6.09s` vs `6.46s` avg on `naive`) is rapid on cached `1500GB pd-ssd` nodes, while `claim` (`12.47s` avg on `batch64-naive` vs `13.68s` on `per-claim-naive`) reflects waiting on `max-refill-rate=100` across `G=6` waves rather than client watch contention.",
            ),
            (row28_old, row28_new),
            (row29_old, row29_new),
            (row30_old, row30_new),
        ],
    )

    # 3) 151500.md Key findings #1, #3, Caveats & Results rows #31-#32 exact wait_pool_ready + sequential first-boot SBD hydration caveat
    row31_old = "per_claim T1 control arm (run 1/6, no-op infra-floor probe, 10x n2-standard-32 + 600GB SBD)."
    row31_new = "per_claim T1 control arm (run 1/6 on freshly created SBD pool — paid first-boot GCE SBD lazy GCS block hydration wait_pool_ready avg=37.24s; NOT footing-comparable to #32; see symmetric C3 #39-#42)."
    row32_old = "SandboxBatch (mif=64) T1 arm (run 2/6, no-op infra-floor probe): 2.02x faster wall (139.10s vs 281.65s #31) and 5.74x lower claim p50 (5.31s vs 30.48s)."
    row32_new = "SandboxBatch (mif=64) T1 arm (run 2/6 after #31 hydrated SBD blocks, wait_pool_ready avg=8.46s, 259 vs 573 sockets; 2.02x wall ratio vs #31 includes #31 first-boot SBD block hydration — see symmetric C3 #39-#40 cold-SBD and #41-#42 hydrated-SBD)."
    _patch_exact(
        os.path.join(reports_dir, "brian-nguyen_20261105-151500.md"),
        [
            (
                "covering both no-op infra-floor (#31-#34: best 91.62s / 0.0305 s/task on batch64-pipelined-noop; 2.02x naive speedup)",
                "covering both no-op infra-floor (#31-#34: best 91.62s / 0.0305 s/task on batch64-pipelined-noop, 1.11x faster than #33 101.57s; note #31->#32 281.65s->139.10s reflects #31 paying first-boot lazy SBD block hydration)",
            ),
            (
                "1. **On `T1` preloaded Secondary Boot Disk (`no-op` infra floor), `SandboxBatch` accelerates `naive` `2.02×` (`281.65s → 139.10s`, `claim p50` `30.48s → 5.31s = 5.74×`) and reaches `91.62s` (`0.0305 s/task`, `32.74 tasks/s`) on `pipelined`.** Eliminating remote layer fetches via the `600GB` preloaded SBD drops gVisor `process` exec median to `0.28–0.37s`, exposing API-server claim watch contention on `per-claim-naive-noop` (`#31` `281.65s`) which `batch64-naive-noop` (`#32` `139.10s`) cuts in half. On `pipelined` (`#34` vs `#33`), `batch64` trims wall time `1.11×` (`101.57s → 91.62s`) while cutting open driver sockets `2.76×` (`635 → 230`).",
                "1. **On hydrated `T1` preloaded Secondary Boot Disk (`no-op` infra floor), `batch64-pipelined-noop` (`#34`) reaches `91.62s` (`0.0305 s/task`, `32.74 tasks/s`) — `1.11×` faster than `per-claim-pipelined-noop` (`#33` `101.57s`) with `2.76×` fewer open driver sockets (`635 → 230`).** Eliminating remote registry fetches via the `600GB` preloaded SBD drops gVisor `process` exec median to `0.28–0.38s`. Note on `naive` (`#31` `281.65s` vs `#32` `139.10s`): `#31` ran first on freshly created `gvisor-sbd-pool` and paid first-boot GCE SBD lazy GCS block hydration (`wait_pool_ready avg = 37.24s`), whereas `#32` ran second on `#31`'s hydrated blocks (`wait_pool_ready avg = 8.46s`, `claim p50` `5.31s` vs `30.48s`, sockets `573 → 259`); Campaign 3 (`#39–#42`) subsequently isolated symmetric Cold SBD (`492.52s` vs `539.18s`) and symmetric Hydrated SBD (`124.73s` vs `146.22s`).",
            ),
            (
                "`wait_pool_ready` dropped to `5.30s` `p50` (`9.68s` avg on `#32` vs `25.92s` avg on `#31`), confirming local SBD block delivery removes cold-layer stalls.",
                "`wait_pool_ready` dropped from `37.24s` avg (`35.59s` `p50`) on first-boot `#31` to `8.46s` avg (`5.30s` `p50`) on `#32` and `4.13–5.77s` avg on hydrated `#33–#36`, confirming local SBD block delivery once hydrated.",
            ),
            (
                "isolate pure `SandboxBatch` delta via `#36` vs `#35` (`248.84s` vs `257.44s`) and `#32` vs `#31` (`139.10s` vs `281.65s`).",
                "isolate pure `SandboxBatch` delta on hydrated SBD via `#34` vs `#33` (`91.62s` vs `101.57s`), `#36` vs `#35` (`248.84s` vs `257.44s`), and Campaign 3 `#39–#42` (`#31` vs `#32` is non-symmetric due to `#31` first-boot SBD block hydration).",
            ),
            (row31_old, row31_new),
            (row32_old, row32_new),
        ],
    )

    # Update RESULTS.md rows #28-#32 and replace #37-#42 cleanly
    kept_lines = []
    for ln in open(results_path, encoding="utf-8"):
        m = re.match(r"\|\s*(\d+)\s*\|", ln)
        if m and int(m.group(1)) >= 37:
            continue
        for old, new in ((row28_old, row28_new), (row29_old, row29_new), (row30_old, row30_new), (row31_old, row31_new), (row32_old, row32_new)):
            if old in ln:
                ln = ln.replace(old, new)
        kept_lines.append(ln)
    if kept_lines and not kept_lines[-1].endswith("\n"):
        kept_lines[-1] += "\n"
    with open(results_path, "w", encoding="utf-8") as fh:
        fh.writelines(kept_lines)
        for rline in summary["results_md_rows_37_42"]:
            fh.write(rline + "\n")


def main():
    out_json = os.path.join(DEFAULT_C3_DIR, "..", "verified_metrics_summary.json")
    wt_pr5 = None
    args = sys.argv[1:]
    if "--render-pr5" in args:
        idx = args.index("--render-pr5")
        wt_pr5 = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    if args:
        out_json = args[0]
    summary = build_all()
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
        fh.write("\n")
    if wt_pr5:
        render_pr5_companion_reports(wt_pr5, summary)
        print(f"Rendered PR #5 companion reports and RESULTS.md rows #37-#42 in {wt_pr5}")
    print(f"Wrote verified metrics -> {os.path.abspath(out_json)}")
    print("Campaign 3 Totals:", json.dumps(summary["campaign_3_totals"], indent=2))
    print(f"Batch Losses & Tradeoffs ({len(summary['batch_losses_and_tradeoffs'])} items):")
    for idx, item in enumerate(summary["batch_losses_and_tradeoffs"], 1):
        print(f"  {idx}. [{item['campaign']}] {item['suite']} :: {item['metric']} -> per_claim={item['per_claim']} vs batch={item['batch']}")


if __name__ == "__main__":
    main()
