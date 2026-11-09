#!/usr/bin/env python3
"""Hydrate 100% of gVisor worker nodes across 1..N clusters with benchmark images via chunked DaemonSets."""
from __future__ import annotations
import argparse, asyncio, inspect, json, logging, os, pathlib, sys, time

import bench_core
from agent_sandbox_rl import AsyncSandboxFleet, FleetConfig
from agent_sandbox_rl.prepull import prepull as do_prepull, prepull_delete as do_prepull_delete


def _wait_zero_prepull_pods(cluster, timeout: float = 180.0) -> int:
    t0 = time.monotonic()
    rem = -1
    while time.monotonic() - t0 < timeout:
        pods = cluster.core_api.list_namespaced_pod(
            cluster.namespace, label_selector="role=prepull"
        ).items
        rem = len(pods)
        if rem == 0:
            return 0
        time.sleep(1.5)
    return rem


def _prepull_one_cluster(cluster, imgs: list[str], sel, rc, tolerations, a) -> dict:
    t0 = time.monotonic()
    sig = inspect.signature(do_prepull)
    kw: dict = {
        "node_selector": sel,
        "wait": True,
        "timeout": a.timeout,
    }
    if "tolerations" in sig.parameters:
        kw["tolerations"] = tolerations
    if "runtime_class" in sig.parameters:
        kw["runtime_class"] = rc
    if "images_per_pod" in sig.parameters:
        kw["images_per_pod"] = a.images_per_pod
    if "max_parallel" in sig.parameters:
        kw["max_parallel"] = a.max_parallel
    if "delete_when_done" in sig.parameters:
        kw["delete_when_done"] = True

    ok = do_prepull(cluster, imgs, **kw)
    do_prepull_delete(cluster)
    rem_pods = _wait_zero_prepull_pods(cluster, timeout=180.0)
    return {
        "cluster": cluster.name,
        "ok": bool(ok) and rem_pods == 0,
        "remaining_prepull_pods": rem_pods,
        "wall_s": round(time.monotonic() - t0, 2),
    }


async def _main(a):
    if not a.in_cluster and not a.context and not a.contexts:
        raise SystemExit("one of --context, --contexts, or --in-cluster is required")
    imgs = [
        ln.strip()
        for ln in pathlib.Path(a.images_file).read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ][:a.problems]
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None
    tolerations = None
    if rc == "gvisor":
        # DaemonSetController evaluates node taints before Pod RuntimeClass admission runs.
        tolerations = [{
            "key": "sandbox.gke.io/runtime",
            "operator": "Equal",
            "value": "gvisor",
            "effect": "NoSchedule",
        }]
    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    cfg = FleetConfig(clusters=clusters, max_concurrent=max(1, len(clusters)))
    af = AsyncSandboxFleet(cfg)
    t0 = time.monotonic()
    try:
        per_cluster = await asyncio.gather(*(
            af._to_thread(_prepull_one_cluster, c, imgs, sel, rc, tolerations, a)
            for c in af.registry
        ))
    finally:
        bench_core.close_fleet(af, wait=True)

    dur = round(time.monotonic() - t0, 2)
    all_ok = all(r["ok"] for r in per_cluster)
    res = {
        "images_file": a.images_file,
        "images_count": len(imgs),
        "clusters_count": len(clusters),
        "images_per_pod": a.images_per_pod,
        "max_parallel": a.max_parallel,
        "ok": all_ok,
        "wall_s": dur,
        "per_cluster": per_cluster,
    }
    print(f"PREPULL DONE: {len(imgs)} images across {len(clusters)} cluster(s) in {dur}s (ok={all_ok})", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2)
    if not all_ok:
        raise SystemExit(2)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="100%-node DaemonSet image prepull (1..N clusters)")
    p.add_argument("--context", default=None)
    p.add_argument("--contexts", default=None, help="comma-separated kubeconfig contexts")
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
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
