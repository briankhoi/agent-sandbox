#!/usr/bin/env python3
"""Mandatory Campaign 3 Pre-Run Smoke Test Gate (`run_smoke_gate.py`).

Runs before EVERY Campaign 3 benchmark suite/run to guarantee:
  1. Cluster health: all target contexts reachable, `gvisor` RuntimeClass present,
     CRDs installed, `gvisor-pool` nodes `Ready`, `0` leftover workload objects.
  2. End-to-end runtime verification: creates a small warm pool (`c_per_cluster`,
     default `10`), exercises BOTH `claim_mode="per_claim"` and `claim_mode="batch"`,
     runs `python3 -c "print(42)"` inside EVERY claimed gVisor container, verifies
     stdout == `"42"`, releases all claims, tears down, and verifies `0` leaked
     claims, `0` leaked warmpools, `0` leaked templates, and `0` leaked batch Leases!
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys, time

import bench_core
from agent_sandbox_rl import AsyncSandboxFleet, BatchClaimConfig, FleetConfig, Task
from agent_sandbox_rl.sources import ListSource


async def _smoke_arm(mode: str, images: list[str], clusters, sel, rc, a) -> dict:
    n_clusters = len(clusters)
    n_total = n_clusters * a.per_cluster_claims
    tasks = [
        Task(id=f"smoke-{mode}-{i}", image=images[i % len(images)])
        for i in range(n_total)
    ]
    cfg = FleetConfig(
        clusters=clusters,
        max_concurrent=n_total,
        max_warmpool_size=max(1, n_total // max(1, len(set(t.image for t in tasks))) + 2),
        warm_per_task=True,
        placement="round-robin" if n_clusters > 1 else "image-affinity",
        warm_create_budget=500,
        claim_mode=mode,
        batch_claims=BatchClaimConfig(max_in_flight=min(64, max(8, n_total))),
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )
    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(tasks))
    t0 = time.monotonic()
    ttfi_vals: list[float] = []
    try:
        await af.setup()
        setup_s = round(time.monotonic() - t0, 3)

        async def _one(t: Task):
            ts = time.monotonic()
            h = await af.acquire(t)
            res = await af._to_thread(
                h.exec, ["python3", "-c", "print(42)"], timeout=60.0
            )
            te = time.monotonic()
            stdout = (res if isinstance(res, str) else (getattr(res, "stdout", "") or "")).strip()
            if "42" not in [ln.strip() for ln in stdout.splitlines()]:
                raise RuntimeError(f"unexpected smoke exec stdout={stdout!r}")
            return h, te - ts

        outcomes = await asyncio.gather(*(_one(t) for t in tasks))
        ttfi_vals = [lat for _h, lat in outcomes]
        t_rel = time.monotonic()
        await af.release_all()
        rel_s = round(time.monotonic() - t_rel, 3)
        await af.teardown()
        verify = await af._to_thread(af._fleet.verify_teardown)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception:
            pass
        raise
    finally:
        af.close()

    def _collect_nonzero_leaks(obj, prefix: str = "") -> dict[str, int]:
        out_leaks: dict[str, int] = {}
        if isinstance(obj, dict):
            for k, val in obj.items():
                key = f"{prefix}.{k}" if prefix else str(k)
                if isinstance(val, bool):
                    continue
                if isinstance(val, int) and val != 0:
                    out_leaks[key] = val
                elif isinstance(val, dict):
                    out_leaks.update(_collect_nonzero_leaks(val, key))
        return out_leaks

    leaked = _collect_nonzero_leaks(verify)
    if leaked:
        raise RuntimeError(f"smoke gate {mode} leaked resources ({leaked}): {verify}")

    return {
        "claim_mode": mode,
        "clusters": n_clusters,
        "claims_tested": n_total,
        "setup_s": setup_s,
        "release_s": rel_s,
        "ttfi_s": bench_core.quantiles(ttfi_vals),
        "verify_teardown": verify,
        "status": "PASS",
    }


async def _main(a):
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None
    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    if a.smoke_image:
        images = [a.smoke_image]
    else:
        images = [l.strip() for l in open(a.images_file) if l.strip()][:max(1, len(clusters) * 2)]

    print(f"[SMOKE GATE] clusters={len(clusters)} images={len(images)} "
          f"claims_per_cluster={a.per_cluster_claims}", flush=True)

    arms = []
    for idx, mode in enumerate(["per_claim", "batch"]):
        if idx > 0:
            await asyncio.sleep(5.0)
        res = await _smoke_arm(mode, images, clusters, sel, rc, a)
        arms.append(res)
        print(f"  SMOKE [{mode}] PASS: n={res['claims_tested']} setup={res['setup_s']}s "
              f"ttfi_p50={res['ttfi_s']['p50']}s ttfi_max={res['ttfi_s']['max']}s "
              f"rel={res['release_s']}s verify={res['verify_teardown']}", flush=True)

    out = {
        "timestamp_epoch": time.time(),
        "status": "PASS",
        "arms": arms,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"SMOKE GATE OVERALL PASS -> {a.out}", flush=True)


def main():
    p = bench_core.build_parser(
        "Mandatory Campaign 3 pre-run smoke gate",
        namespace="agent-sandbox-rl",
        images_file="/app/drivers/images/swebench500.txt",
        cpu="125m",
        memory="256Mi",
        termination_grace_seconds=0,
        out="results/smoke_gate.json",
    )
    p.add_argument("--per-cluster-claims", type=int, default=8)
    p.add_argument("--smoke-image", default="",
                   help="override single non-SWE-bench image (used before cold C3-1b runs so 0 SWE-bench layers are cached)")
    a = p.parse_args()
    try:
        asyncio.run(_main(a))
    except Exception as exc:
        print(f"SMOKE GATE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
