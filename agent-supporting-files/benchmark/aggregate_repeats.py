#!/usr/bin/env python3
"""Aggregate N repeats of test/stress output dirs and report mean/stdev.

Usage:
    python3 aggregate_repeats.py <label> <dir1> <dir2> ... <dirN>

Reuses the same correctly-keyed-by-full-label-set delta logic as analyze_ab.py.
"""
import gzip
import json
import statistics
import sys
from datetime import datetime


def series_deltas(path, metric_name, resource_filter=None):
    first, last = {}, {}
    with gzip.open(path, "rt") as f:
        for line in f:
            d = json.loads(line)
            if d["metric"] != metric_name:
                continue
            labels = d.get("labels", {})
            if resource_filter is not None and labels.get("resource") != resource_filter:
                continue
            key = tuple(sorted((k, v) for k, v in labels.items() if k != "resource"))
            v = d["value"]
            if key not in first:
                first[key] = v
            last[key] = v
    return {k: last[k] - first[k] for k in first}


def one_run(stress_dir):
    metrics_path = f"{stress_dir}/metrics.jsonl.gz"
    summary = json.load(open(f"{stress_dir}/summary.json"))
    total_created = sum(p.get("created", 0) for p in summary["phases"])
    start = datetime.fromisoformat(summary["startTime"])
    end = datetime.fromisoformat(summary["endTime"])

    patch = series_deltas(metrics_path, "apiserver_request_total", resource_filter="sandboxes")
    patch_obj = patch_status = 0
    for labels, delta in patch.items():
        ld = dict(labels)
        if ld.get("verb") != "PATCH":
            continue
        if ld.get("subresource", "") == "":
            patch_obj += delta
        elif ld.get("subresource") == "status":
            patch_status += delta

    reconcile = series_deltas(metrics_path, "controller_runtime_reconcile_total")
    sandbox_reconciles = sum(d for l, d in reconcile.items() if dict(l).get("controller") == "sandbox")

    rsum = series_deltas(metrics_path, "controller_runtime_reconcile_time_seconds_sum")
    rcount = series_deltas(metrics_path, "controller_runtime_reconcile_time_seconds_count")
    s_sum = sum(d for l, d in rsum.items() if dict(l).get("controller") == "sandbox")
    s_count = sum(d for l, d in rcount.items() if dict(l).get("controller") == "sandbox")

    # throughput phase end-to-end ready p50, straight from summary.json
    tp = next((p for p in summary["phases"] if p["name"].startswith("throughput")), None)
    e2e_p50_ms = tp["latency"]["endToEndReady"]["p50Ms"] if tp else None

    return {
        "duration_s": (end - start).total_seconds(),
        "created": total_created,
        "patch_obj_per_sandbox": patch_obj / total_created,
        "patch_status_per_sandbox": patch_status / total_created,
        "reconciles_per_sandbox": sandbox_reconciles / total_created,
        "avg_reconcile_ms": (1000 * s_sum / s_count) if s_count else None,
        "e2e_ready_p50_ms": e2e_p50_ms,
    }


def fmt_stats(values):
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return f"{mean:.4f} +/- {sd:.4f}  (n={len(values)}, values={[round(v,3) for v in values]})"


def main():
    label = sys.argv[1]
    dirs = sys.argv[2:]
    runs = [one_run(d) for d in dirs]
    print(f"=== {label} ({len(dirs)} repeats) ===")
    for key in ["duration_s", "created", "patch_obj_per_sandbox", "patch_status_per_sandbox",
                "reconciles_per_sandbox", "avg_reconcile_ms", "e2e_ready_p50_ms"]:
        vals = [r[key] for r in runs if r[key] is not None]
        print(f"  {key:28s}: {fmt_stats(vals)}")
    print()


if __name__ == "__main__":
    main()
