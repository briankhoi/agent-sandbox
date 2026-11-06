#!/usr/bin/env python3
"""SWE-bench Verified 500x6 = 3,000 (or 500x1 cold) benchmark driver for Campaign 3."""
from __future__ import annotations
import asyncio
import bench_core


def main():
    p = bench_core.build_parser(
        "SWE-bench Verified benchmark driver (Campaign 3)",
        namespace="agent-sandbox-rl",
        problems=500,
        rollouts=6,
        concurrency=500,
        cpu="125m",
        memory="1Gi",
        probe="true",
        termination_grace_seconds=0,
        warm_create_budget=1000,
        out="results/swebench_3000.json",
    )
    a = p.parse_args()
    a._driver = __file__
    asyncio.run(bench_core.run_bench(a))


if __name__ == "__main__":
    main()
