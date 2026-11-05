#!/usr/bin/env python3
"""Flexible SWE-bench benchmark driver (recycle on by default; RESULTS.md #15-#16)."""
import asyncio
from bench_core import build_parser, run_bench


def main():
    p = build_parser(
        "Flexible SWE-bench benchmark (all knobs; recycle on)",
        problems=500, rollouts=6, concurrency=500, claim_concurrency=500,
        cpu="125m", memory="1Gi", probe="true", enable_cow=False,
        warm_create_budget=1000, overcommit_factor=3.5,
        strategies="naive", recycle=True,
        out="results/recycle_bench.json")
    a = p.parse_args()
    a._driver = "run_recycle_bench.py"
    asyncio.run(run_bench(a))


if __name__ == "__main__":
    main()
