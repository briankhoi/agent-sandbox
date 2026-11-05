#!/usr/bin/env python3
"""Hydrate 100% of gVisor worker nodes with every benchmark image via chunked DaemonSets."""
from __future__ import annotations
import argparse, json, logging, os, pathlib, sys, time

from agent_sandbox_rl import ClusterConfig, FleetConfig, SandboxFleet
from agent_sandbox_rl.prepull import prepull as do_prepull, prepull_delete as do_prepull_delete


def _parse_selector(s):
    if not s or "=" not in s:
        return None
    k, v = s.split("=", 1)
    return {k: v}


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="100%-node DaemonSet image prepull")
    p.add_argument("--context", default=None)
    p.add_argument("--in-cluster", action="store_true")
    p.add_argument("--namespace", default="agent-sandbox-rl")
    p.add_argument("--node-selector", default="cloud.google.com/gke-nodepool=gvisor-pool")
    p.add_argument("--runtime-class", default="gvisor")
    p.add_argument("--images-file", required=True)
    p.add_argument("--problems", type=int, default=500)
    p.add_argument("--images-per-pod", type=int, default=25)
    p.add_argument("--max-parallel", type=int, default=5)
    p.add_argument("--timeout", type=int, default=2400)
    p.add_argument("--out", default="results/prepull.json")
    a = p.parse_args()

    imgs = [ln.strip() for ln in pathlib.Path(a.images_file).read_text().splitlines() if ln.strip()][:a.problems]
    sel = _parse_selector(a.node_selector)
    tolerations = None
    if a.runtime_class == "gvisor":
        # DaemonSetController evaluates node taints before Pod RuntimeClass admission runs.
        tolerations = [{
            "key": "sandbox.gke.io/runtime",
            "operator": "Equal",
            "value": "gvisor",
            "effect": "NoSchedule",
        }]
    cfg = FleetConfig(
        clusters=[ClusterConfig(
            name="bench",
            context=a.context,
            in_cluster=a.in_cluster,
            namespace=a.namespace,
            node_selector=sel,
            runtime_class=a.runtime_class or None,
        )],
    )
    t0 = time.monotonic()
    with SandboxFleet(cfg) as fleet:
        cluster = fleet.registry.get("bench")
        ok = do_prepull(
            cluster,
            imgs,
            node_selector=sel,
            tolerations=tolerations,
            runtime_class=a.runtime_class or None,
            wait=True,
            timeout=a.timeout,
            images_per_pod=a.images_per_pod,
            max_parallel=a.max_parallel,
            delete_when_done=True,
        )
        do_prepull_delete(cluster)
    dur = round(time.monotonic() - t0, 2)
    res = {
        "images_file": a.images_file,
        "images_count": len(imgs),
        "images_per_pod": a.images_per_pod,
        "max_parallel": a.max_parallel,
        "ok": ok,
        "wall_s": dur,
    }
    print(f"PREPULL DONE: {len(imgs)} images across all nodes in {dur}s (ok={ok})", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2)
    if not ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
