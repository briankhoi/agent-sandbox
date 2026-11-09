#!/usr/bin/env python3
"""High-Density Active Sandbox Packing Sweep (`run_density_bench.py`).

Tests active concurrent real SWE-bench Verified gVisor sandboxes across density bars:
  * `2.0 / vCPU` (`c = 640` on `320` worker vCPUs)
  * `5.0 / vCPU` (`c = 1,600` on `320` worker vCPUs — Eval Pass bar)
  * `7.34 / vCPU` (`c = 2,350` on `320` worker vCPUs = `235` active user pods/node + ~16 system pods <= 256 maxPods)

Uses the Eval Doc §3 container profile (`cpu=125m`, `memory=256Mi`, `4Gi` emptyDir at `/tmp`)
and executes a real Python workload inside EVERY active sandbox while all `c` sandboxes
are held simultaneously, auditing node conditions (`Ready`, `MemoryPressure`, `DiskPressure`,
`PIDPressure`) and pod container statuses (`OOMKilled`, `Evicted`) before measuring bulk release.
"""
from __future__ import annotations
import asyncio, json, math, os, time

import bench_core
import run_claim_soak_bench as burst_mod
from agent_sandbox_rl import AsyncSandboxFleet, FleetConfig, Task
from agent_sandbox_rl.sources import ListSource


def _audit_cluster_health(af: AsyncSandboxFleet, active_pod_names: set[str], pool_label: str = "gvisor-pool") -> dict:
    not_ready_nodes = 0
    mem_pressure_nodes = 0
    disk_pressure_nodes = 0
    pid_pressure_nodes = 0
    total_nodes = 0
    oom_kills = 0
    evicted_pods = 0
    running_active_pods = 0

    for c in af.registry:
        nodes = c.core_api.list_node(label_selector=f"cloud.google.com/gke-nodepool={pool_label}").items
        total_nodes += len(nodes)
        for n in nodes:
            conds = {cd.type: cd.status for cd in (n.status.conditions or [])}
            if conds.get("Ready") != "True":
                not_ready_nodes += 1
            if conds.get("MemoryPressure") == "True":
                mem_pressure_nodes += 1
            if conds.get("DiskPressure") == "True":
                disk_pressure_nodes += 1
            if conds.get("PIDPressure") == "True":
                pid_pressure_nodes += 1

        pods = c.core_api.list_namespaced_pod(c.namespace).items
        for p in pods:
            pname = p.metadata.name
            if pname not in active_pod_names:
                continue
            if p.status and p.status.phase == "Running":
                running_active_pods += 1
            if p.status and p.status.reason == "Evicted":
                evicted_pods += 1
            for cs in (p.status.container_statuses or []) if p.status else []:
                for st_obj in (cs.state, cs.last_state):
                    term = getattr(st_obj, "terminated", None)
                    if term and getattr(term, "reason", None) == "OOMKilled":
                        oom_kills += 1

    return {
        "worker_nodes": total_nodes,
        "not_ready_nodes": not_ready_nodes,
        "memory_pressure_nodes": mem_pressure_nodes,
        "disk_pressure_nodes": disk_pressure_nodes,
        "pid_pressure_nodes": pid_pressure_nodes,
        "active_pods_checked": len(active_pod_names),
        "running_active_pods": running_active_pods,
        "oom_kills": oom_kills,
        "evicted_pods": evicted_pods,
    }


async def _run_density_point(c_target: int, images_all: list[str], a, sel, rc, pool_label: str) -> dict:
    n_imgs = min(len(images_all), c_target)
    images = images_all[:n_imgs]
    reps = math.ceil(c_target / n_imgs)
    plan_tasks = [
        Task(id=f"dens-{c_target}-{i}-r{r}", image=images[i])
        for r in range(reps)
        for i in range(n_imgs)
    ][:c_target]

    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    n_clusters = max(1, len(clusters))
    wcb = getattr(a, "warm_create_budget", 600)

    cfg_kwargs = dict(
        clusters=clusters,
        max_concurrent=c_target,
        max_warmpool_size=reps,
        warm_per_task=True,
        placement=a.placement,
        warm_create_budget=max(0, int(wcb)),
        overcommit_factor=max(4.0, a.overcommit_factor),
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )
    bench_core.apply_claim_mode_kwargs(cfg_kwargs, a)
    cfg = FleetConfig(**cfg_kwargs)

    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(plan_tasks))
    density_target = round(c_target / a.worker_vcpus, 3)
    mif = "n/a"
    if getattr(cfg, "batch_claims", None):
        mif = getattr(cfg.batch_claims, "max_in_flight", "default(20)")
    print(f"[density c={c_target} ({density_target}/vCPU)] mode={getattr(a, 'claim_mode', 'per_claim')}(mif={mif}) "
          f"images={n_imgs} max_wp={reps} cpu={a.cpu} mem={a.memory} tmp={a.tmp_disk_gi}Gi",
          flush=True)

    setup_t0 = time.monotonic()
    verify_out = None
    reap_info = None
    try:
        await af.setup()
        w_info = await burst_mod._wait_min_ready(af, len(plan_tasks), timeout=900.0, require_zero_terminating=True)
        setup_s = round(time.monotonic() - setup_t0, 2)
        if a.post_warm_settle_s > 0:
            await asyncio.sleep(a.post_warm_settle_s)

        proc_before = burst_mod.detailed_proc_stats()
        exec_sem = asyncio.Semaphore(max(1, a.exec_concurrency))

        t_burst = time.monotonic()
        outcomes = await asyncio.gather(*(
            burst_mod._acquire_and_probe(af, t, t_burst, True, a.probe, exec_sem)
            for t in plan_tasks
        ))
        burst_and_exec_wall_s = time.monotonic() - t_burst
        proc_held = burst_mod.detailed_proc_stats()

        ok_rows = [o for o in outcomes if o["ok"]]
        err_rows = [o for o in outcomes if not o["ok"]]
        handles = [o["handle"] for o in ok_rows if o["handle"] is not None]
        active_pods = {h.pod_name for h in handles if getattr(h, "pod_name", None)}

        ready_offsets = [o["ready_offset_s"] for o in ok_rows if o["ready_offset_s"] is not None]
        claim_wall_s = max(ready_offsets) if ready_offsets else burst_and_exec_wall_s
        wire_ok = [o["wire_claim_s"] for o in ok_rows]
        cmd_ok = [o["first_cmd_exec_s"] for o in ok_rows if o["first_cmd_exec_s"] is not None]
        ttfi_wall = [o["ttfi_wall_s"] for o in ok_rows if o["ttfi_wall_s"] is not None]
        ttfi_inline = [o["ttfi_inline_s"] for o in ok_rows if o["ttfi_inline_s"] is not None]
        probe_errs = [o["probe_err"] for o in ok_rows if o["probe_err"] is not None]
        errs = [o["err"] for o in err_rows if o["err"] is not None]

        # Audit node & active-pod health while ALL c sandboxes are simultaneously active
        health = await af._to_thread(_audit_cluster_health, af, active_pods, pool_label)

        # Measure bulk release throughput of all c active sandboxes
        t_rel = time.monotonic()
        await af.release_all()
        release_wall_s = time.monotonic() - t_rel
        proc_after_rel = burst_mod.detailed_proc_stats()

        t_td = time.monotonic()
        await af.teardown()
        teardown_s = round(time.monotonic() - t_td, 3)
        verify_out = await bench_core.safe_verify_teardown(af)
        reap_info = await bench_core.wait_clusters_drained(clusters, timeout=240.0, settle_s=0.0)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception as td_exc:
            print(f"[density c={c_target}] cleanup warning after failure: {td_exc}", flush=True)
        raise
    finally:
        bench_core.close_fleet(af, wait=False)

    active_ok = len(ok_rows) - len(probe_errs)
    batch_mif = None
    if getattr(cfg, "batch_claims", None):
        batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)
    claims_rate = round(len(wire_ok) / claim_wall_s, 2) if claim_wall_s > 0 else None
    rel_rate = round(len(wire_ok) / release_wall_s, 2) if release_wall_s > 0 else None

    return {
        "target_concurrency": c_target,
        "clusters": n_clusters,
        "worker_vcpus": a.worker_vcpus,
        "target_sandboxes_per_vcpu": density_target,
        "achieved_active_sandboxes_per_vcpu": round(active_ok / a.worker_vcpus, 3),
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "batch_max_in_flight": batch_mif,
        "setup_s": setup_s,
        "warm_wait": w_info,
        "claim_burst_wall_s": round(claim_wall_s, 3),
        "claims_per_s": claims_rate,
        "per_cluster_claims_per_s": round(claims_rate / n_clusters, 2) if claims_rate is not None else None,
        "burst_and_exec_wall_s": round(burst_and_exec_wall_s, 3),
        "ok_claims": len(wire_ok),
        "ok_active_exec": active_ok,
        "claim_err": len(errs),
        "probe_err": len(probe_errs),
        "failure_rate_pct": round(100.0 * (len(errs) + len(probe_errs)) / c_target, 5),
        "wire_claim_s": bench_core.quantiles(wire_ok),
        "first_cmd_exec_s": bench_core.quantiles(cmd_ok),
        "ttfi_s": bench_core.quantiles(ttfi_wall),
        "ttfi_inline_s": bench_core.quantiles(ttfi_inline),
        "release_wall_s": round(release_wall_s, 3),
        "release_sandboxes_per_s": rel_rate,
        "per_cluster_release_sandboxes_per_s": round(rel_rate / n_clusters, 2) if rel_rate is not None else None,
        "teardown_s": teardown_s,
        "reap_zero_pods": reap_info,
        "cluster_health_while_held": health,
        "proc_before": proc_before,
        "proc_held": proc_held,
        "proc_after_release": proc_after_rel,
        "verify_teardown": verify_out,
        "errors_sample": (errs + probe_errs)[:8],
    }


async def _main(a):
    images = [l.strip() for l in open(a.images_file) if l.strip()][:a.problems]
    sel = bench_core._parse_selector(a.node_selector)
    pool_label = (sel or {}).get("cloud.google.com/gke-nodepool", "gvisor-pool")
    rc = a.runtime_class or None
    concs = [int(x.strip()) for x in a.concurrencies.split(",") if x.strip()]

    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    points = []
    out_file = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    for idx, c_val in enumerate(concs):
        if idx > 0:
            drain_info = await bench_core.wait_clusters_drained(clusters, timeout=240.0, settle_s=30.0)
            print(f"  [inter-density drain] {drain_info}", flush=True)
        pt = await _run_density_point(c_val, images, a, sel, rc, pool_label)
        points.append(pt)
        h = pt["cluster_health_while_held"]
        print(
            f"RESULT DENSITY c={c_val} ({pt['achieved_active_sandboxes_per_vcpu']}/vCPU) "
            f"mode={a.claim_mode}: ok={pt['ok_active_exec']}/{c_val} "
            f"claim_wall={pt['claim_burst_wall_s']}s ({pt['claims_per_s']}/s) "
            f"ttfi(first={pt['ttfi_s']['first']} p50={pt['ttfi_s']['p50']} "
            f"p99={pt['ttfi_s']['p99']} max={pt['ttfi_s']['max']}) "
            f"rel={pt['release_wall_s']}s ({pt['release_sandboxes_per_s']}/s) "
            f"oom={h['oom_kills']} evict={h['evicted_pods']} socks={pt['proc_held']['open_sockets']}",
            flush=True,
        )
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump({"params": vars(a), "points": points}, fh, indent=2)
    print(f"-> {a.out}", flush=True)


def main():
    p = bench_core.build_parser(
        "High-density active SWE-bench gVisor packing sweep",
        namespace="agent-sandbox-rl",
        problems=500,
        cpu="125m",
        memory="256Mi",
        tmp_disk_gi=4,
        probe="python3 -c 'import os, sys; assert os.path.isdir(\"/testbed\"); print(sys.version_info[0])'",
        termination_grace_seconds=0,
        warm_create_budget=600,
        out="results/density_sweep.json",
    )
    p.add_argument("--concurrencies", default="640,1600,2350")
    p.add_argument("--worker-vcpus", type=int, default=320)
    p.add_argument("--post-warm-settle-s", type=float, default=5.0)
    p.add_argument("--exec-concurrency", type=int, default=512)
    a = p.parse_args()
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
