#!/usr/bin/env python3
"""Runs non-default (P images x G tasks) shapes on the existing 640 warm pools in rl-bench:
  - w500:  500 images x 1 task   =  500 sandboxes (SWE-bench 1:1 evaluation shape)
  - w1024:  16 images x 64 tasks = 1024 sandboxes (deep-group GRPO RL shape, G > P)
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("RL_BENCH_SIZES", "2560")  # POOLS = 2560 // 4 = 640
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))
import claim_mode_bench as kb  # noqa: E402
import gke_scale_bench as gb   # noqa: E402

SHAPES = [
    gb.Shape(size=500, warm=True, per_image=1),
    gb.Shape(size=1024, warm=True, per_image=64),
]
VARIANTS = [v for v in gb.WARM_VARIANTS
            if v.name in ("per-claim", "batch-unbounded", "batch-default")]


def custom_fleet_config(args, variant, shape):
    return kb.fleet_config(
        args, variant,
        clusters=[gb.cluster_config(args)],
        max_concurrent=max(shape.tasks, 1),
        max_warmpool_size=max(8, shape.per_image),
    )


def custom_run_plan():
    plan = []
    for shape in SHAPES:
        for v, k in gb.interleave(VARIANTS, gb.RUNS):
            plan.append(gb.PlannedRun(f"{shape.name}-{v.name}-run{k}", v, k, shape, gb.STEPS))
    return plan


gb.fleet_config = custom_fleet_config
gb.run_plan = custom_run_plan

if __name__ == "__main__":
    sys.exit(gb.main())
