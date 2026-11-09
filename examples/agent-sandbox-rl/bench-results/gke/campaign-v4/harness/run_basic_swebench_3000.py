#!/usr/bin/env python3
"""Basic SWE-bench throughput benchmark — the canonical run.

500 SWE-bench Verified images x 6 tasks/image = 3000 tasks, 500-wide, shallow warm
(one replica per image). By default it **sweeps the SDK warm-pool strategies**
(naive / sliding / pipelined) via `fleet.run(strategy=…)` — a fresh warm claim per
task — and reports a per-variant comparison. **Recycle** (reset-and-reuse, ÷G
claims) is an orthogonal opt-in: add `--recycle` to also run a recycle variant per
strategy. Merged-SDK-only (engine in bench_core.py).

This is the run everyone should produce first — the T0/T1 comparable numbers. For
non-standard shapes (other image counts, cycles, real probes) use run_recycle_bench.py.

Usage (canonical — sweeps naive,sliding,pipelined):
  PYTHONPATH=<sdk>:<sdk>/tests python run_basic_swebench_3000.py \
    --context <ctx> --namespace <ns> \
    --node-selector cloud.google.com/gke-nodepool=<pool> \
    --images-file swebench500_digests.txt
  # add --recycle to also measure the reset-and-reuse variant per strategy
  # add --enable-cow (or --cow) to enable OverlayFS CoW RAM mounts
"""
import asyncio
from bench_core import build_parser, run_bench


def main():
    p = build_parser(
        "Basic SWE-bench benchmark: 500 images x 6 = 3000 tasks, strategy sweep",
        problems=500, rollouts=6, concurrency=500, claim_concurrency=500,
        cpu="125m", memory="1Gi", probe="true", enable_cow=False,
        strategies="naive,sliding,pipelined",
        out="results/basic_swebench_3000.json")
    asyncio.run(run_bench(p.parse_args()))


if __name__ == "__main__":
    main()
