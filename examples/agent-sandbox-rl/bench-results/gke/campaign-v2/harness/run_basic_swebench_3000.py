#!/usr/bin/env python3
"""Basic SWE-bench throughput benchmark — the canonical 500 x 6 = 3000 sweep."""
import asyncio
from bench_core import build_parser, run_bench


def main():
    p = build_parser(
        "Basic SWE-bench benchmark: 500 images x 6 = 3000 tasks, strategy sweep",
        problems=500, rollouts=6, concurrency=500, claim_concurrency=500,
        cpu="125m", memory="1Gi", probe="true", enable_cow=False,
        warm_create_budget=1000, overcommit_factor=3.5,
        strategies="naive,sliding,pipelined",
        out="results/basic_swebench_3000.json")
    a = p.parse_args()
    a._driver = "run_basic_swebench_3000.py"
    asyncio.run(run_bench(a))


if __name__ == "__main__":
    main()
