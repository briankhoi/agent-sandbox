#!/usr/bin/env python3
"""Compare two test/stress metrics.jsonl.gz runs (baseline vs PR branch).

Usage:
    python3 analyze_ab.py <baseline-output-dir> <pr-output-dir>

Each dir must contain metrics.jsonl.gz and summary.json as written by
`go run ./test/stress --output-dir=<dir> ...`.

IMPORTANT: a Prometheus counter series is uniquely identified by its FULL
label set (metric name + every label). Diffing "first sample seen" vs "last
sample seen" for a metric name while ignoring some of its labels silently
blends multiple distinct series together and produces a meaningless number.
This script's first draft made exactly that mistake for
apiserver_request_total (it has both subresource="" and subresource="status"
series under resource="sandboxes", verb="PATCH") -- fixed here by grouping on
the full (verb, subresource) key before computing any delta.
"""
import gzip
import json
import sys
from datetime import datetime


def series_deltas(path, metric_name, resource_filter=None):
    """Returns {full_label_tuple: delta} for one metric name.

    full_label_tuple includes every label on the sample except `resource`
    (which is pre-filtered), so distinct series are never blended.
    """
    first = {}
    last = {}
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


def summarize(label, stress_dir):
    metrics_path = f"{stress_dir}/metrics.jsonl.gz"
    summary_path = f"{stress_dir}/summary.json"

    summary = json.load(open(summary_path))
    total_created = sum(p.get("created", 0) for p in summary["phases"])
    start = datetime.fromisoformat(summary["startTime"])
    end = datetime.fromisoformat(summary["endTime"])

    patch_deltas = series_deltas(metrics_path, "apiserver_request_total", resource_filter="sandboxes")
    patch_by_subresource = {}
    for (labels, delta) in patch_deltas.items():
        ld = dict(labels)
        if ld.get("verb") != "PATCH":
            continue
        patch_by_subresource[ld.get("subresource", "")] = delta

    reconcile_deltas = series_deltas(metrics_path, "controller_runtime_reconcile_total")
    sandbox_reconciles = sum(
        delta for (labels, delta) in reconcile_deltas.items()
        if dict(labels).get("controller") == "sandbox"
    )

    rtime_sum = series_deltas(metrics_path, "controller_runtime_reconcile_time_seconds_sum")
    rtime_count = series_deltas(metrics_path, "controller_runtime_reconcile_time_seconds_count")
    sandbox_rtime_sum = sum(d for (l, d) in rtime_sum.items() if dict(l).get("controller") == "sandbox")
    sandbox_rtime_count = sum(d for (l, d) in rtime_count.items() if dict(l).get("controller") == "sandbox")

    print(f"=== {label} ({stress_dir}) ===")
    print(f"  run duration:                     {(end-start).total_seconds():.1f}s")
    print(f"  sandboxes created (all phases):   {total_created}")
    print(f"  PATCH sandboxes, subresource='':   {patch_by_subresource.get('', 0)}"
          f"  ({patch_by_subresource.get('', 0)/total_created:.3f} / sandbox)")
    print(f"  PATCH sandboxes, subresource=status: {patch_by_subresource.get('status', 0)}"
          f"  ({patch_by_subresource.get('status', 0)/total_created:.3f} / sandbox)")
    print(f"  sandbox controller reconciles:     {sandbox_reconciles}"
          f"  ({sandbox_reconciles/total_created:.2f} / sandbox)")
    if sandbox_rtime_count:
        print(f"  avg reconcile duration:            {1000*sandbox_rtime_sum/sandbox_rtime_count:.3f} ms")
    print()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    summarize("BASELINE", sys.argv[1])
    summarize("PR", sys.argv[2])
