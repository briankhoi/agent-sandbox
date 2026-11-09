#!/usr/bin/env python3
"""Full hicard AR dataset benchmark — every hicard image, shallow.

Warms one sandbox per hicard image (~20,195 distinct images across the five hicard
sub-datasets: r2e-gym, swe-rebench, swe-gym, swebench-full, swe_rebench_leaderboard)
and runs each image's tasks. This is the high-cardinality stress case: ~20K
concurrent shallow sandboxes, which needs a large gVisor cluster — mind the scale
watch-outs (per-node disk for cold pulls, pod-CIDR / maxPods, apiserver 429). At this
cardinality it defaults to the **`pipelined`** strategy alone (footprint-bounded);
add `--strategies naive,pipelined` or `--recycle` for a wider (costlier) sweep.

Get the image list with `drivers/gen_hicard_images.sh` (writes hicard_images.txt),
then run via the guard so the watchdog can catch a runaway:

  DRIVER=run_hicard_20k_full.py THRESHOLD=45000 CONC=20195 \
    CTX=... NS=... POOL=... ASRL_PKG=... IMAGES=hicard_images.txt \
    drivers/guard_async.sh

Defaults: ~20,195 images x 6 tasks = ~121K tasks, 20K-wide, cpu=150m mem=256Mi.
(~20K held + replenishment => plan for ~2x pods; set the guard THRESHOLD well above.)
"""
import asyncio
from bench_core import build_parser, run_bench


def main():
  p = build_parser(
      "Full hicard benchmark: all ~20K hicard images, shallow, pipelined",
      problems=20195, rollouts=6, concurrency=20195, claim_concurrency=500,
      cpu="150m", memory="256Mi", probe="true", strategies="pipelined",
      images_file="hicard_images.txt", out="results/hicard_20k_full.json")
  asyncio.run(run_bench(p.parse_args()))


if __name__ == "__main__":
  main()
