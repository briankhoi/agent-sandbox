#!/usr/bin/env python3
"""Pre-run smoke test gate (`run_smoke_gate.py`).

Runs before benchmark suites to guarantee:
  1. Cluster health: target context(s) reachable, `gvisor` RuntimeClass present,
     CRDs installed, worker nodes `Ready`, 0 leftover workload objects.
  2. End-to-end runtime verification: creates a small warm pool (`c_per_cluster`),
     exercises claim modes (`per_claim` and, if available, `batch`), runs
     `python3 -c "print(42)"` inside EVERY claimed container, verifies stdout == `"42"`,
     releases claims, tears down, and verifies zero leaked resources.
"""
from __future__ import annotations
import argparse, asyncio, json, os, sys, time

import bench_core
from agent_sandbox_rl import AsyncSandboxFleet, FleetConfig, Task
from agent_sandbox_rl.sources import ListSource


def _find_teardown_leaks(verify: dict | None) -> dict:
    """Inspect `SandboxFleet.verify_teardown()` (`{cluster: {..., 'remaining': {...}}}`) for errors or survivors."""
    leaked: dict = {}
    for cname, entry in (verify or {}).items():
        if isinstance(entry, int):
            if entry != 0:
                leaked[cname] = entry
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("error"):
            leaked[cname] = {"error": entry["error"]}
            continue
        rem = entry.get("remaining")
        if isinstance(rem, dict):
            nz = {k: v for k, v in rem.items() if isinstance(v, int) and v > 0}
            if nz:
                leaked[cname] = nz
        else:
            nz = {k: v for k, v in entry.items() if isinstance(v, int) and v > 0}
            if nz:
                leaked[cname] = nz
    return leaked


async def _smoke_arm(mode: str, images: list[str], clusters, sel, rc, a) -> dict:
    n_clusters = len(clusters)
    n_total = n_clusters * a.per_cluster_claims
    if mode == "batch" and bench_core.BatchClaimConfig is None:
        print("  SMOKE [batch] SKIP: BatchClaimConfig not available in installed SDK", flush=True)
        return {
            "claim_mode": mode,
            "clusters": n_clusters,
            "claims_tested": 0,
            "setup_s": 0.0,
            "release_s": 0.0,
            "ttfi_s": bench_core.quantiles([]),
            "verify_teardown": {},
            "status": "SKIPPED",
        }

    tasks = [
        Task(id=f"smoke-{mode}-{i}", image=images[i % len(images)])
        for i in range(n_total)
    ]
    cfg_kwargs = dict(
        clusters=clusters,
        max_concurrent=n_total,
        max_warmpool_size=max(1, n_total // max(1, len(set(t.image for t in tasks))) + 2),
        warm_per_task=True,
        placement="round-robin" if n_clusters > 1 else "image-affinity",
        warm_create_budget=500,
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )
    if bench_core._has_field(FleetConfig, "claim_mode"):
        cfg_kwargs["claim_mode"] = mode
    if bench_core._has_field(FleetConfig, "batch_claims") and bench_core.BatchClaimConfig is not None:
        cfg_kwargs["batch_claims"] = bench_core.BatchClaimConfig(max_in_flight=min(64, max(8, n_total)))
    cfg = FleetConfig(**cfg_kwargs)

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
        verify = await bench_core.safe_verify_teardown(af)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception as td_exc:
            print(f"  SMOKE [{mode}] cleanup warning after failure: {td_exc}", file=sys.stderr, flush=True)
        raise
    finally:
        bench_core.close_fleet(af, wait=True)

    if isinstance(verify, dict) and verify.get("error"):
        raise RuntimeError(f"smoke gate {mode} verify_teardown failed: {verify['error']}")
    leaked = _find_teardown_leaks(verify)
    if leaked:
        raise RuntimeError(f"smoke gate {mode} leaked resources: {leaked} (raw={verify})")

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
    modes = ["per_claim"]
    if bench_core.BatchClaimConfig is not None:
        modes.append("batch")

    for idx, mode in enumerate(modes):
        if idx > 0:
            await bench_core.wait_clusters_drained(clusters, timeout=120.0, settle_s=5.0)
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
    default_imgs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "swebench500.txt")
    p = bench_core.build_parser(
        "Pre-run smoke gate",
        namespace="agent-sandbox-rl",
        images_file=default_imgs,
        cpu="125m",
        memory="256Mi",
        termination_grace_seconds=0,
        out="results/smoke_gate.json",
    )
    p.add_argument("--per-cluster-claims", type=int, default=8)
    p.add_argument("--smoke-image", default="",
                   help="override single non-SWE-bench image (used before cold runs)")
    a = p.parse_args()
    try:
        asyncio.run(_main(a))
    except Exception as exc:
        print(f"SMOKE GATE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
