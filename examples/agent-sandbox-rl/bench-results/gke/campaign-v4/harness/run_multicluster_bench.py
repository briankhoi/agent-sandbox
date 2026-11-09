#!/usr/bin/env python3
"""Multi-Cluster Fleet Benchmark (`run_multicluster_bench.py`).

Drives multiple regional GKE clusters simultaneously from a single Python process
(`FleetConfig(clusters=[c1..cN], placement="round-robin")`) across two primary scenarios:
  1. `--suite warm-burst`:
     Standing warm sandboxes balanced across clusters, measuring BOTH fleet-wide (`N x`)
     AND per-cluster (`÷ N`) claims/s, paired TTFI, client open socket counts, bulk release
     API throughput (`release_wall_s`), and full zero-pod GC convergence (`reap_zero_pods`).
  2. `--suite golden-small`:
     Multiple staggered concurrent tenant RL jobs (e.g. 8 jobs x 512 rollouts = 4,096 rollouts)
     sharing a multi-cluster fleet, measuring per-job step barrier P100, aggregate failure rate,
     and cross-cluster isolation.
"""
from __future__ import annotations
import asyncio, collections, json, os, time

import bench_core
import run_claim_soak_bench as burst_mod
from agent_sandbox_rl import AsyncSandboxFleet, FleetConfig, Task
from agent_sandbox_rl.sources import ListSource


async def _run_multicluster_warm_burst(a, images: list[str], sel, rc) -> dict:
    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    n_clusters = max(1, len(clusters))
    concs = [int(x.strip()) for x in a.concurrencies.split(",") if x.strip()]
    max_c = max(concs)
    total_warm = len(images) * a.replicas_per_image
    if max_c > total_warm:
        raise SystemExit(f"max concurrency {max_c} > total_warm {total_warm}")

    # Interleave across images so any prefix `all_tasks[:c]` is balanced across clusters
    all_tasks = [
        Task(id=f"mc-{bench_core._sid(img)}-r{r}", image=img)
        for r in range(a.replicas_per_image)
        for img in images
    ]

    wcb = getattr(a, "warm_create_budget", 2500)
    cfg_kwargs = dict(
        clusters=clusters,
        max_concurrent=max_c,
        max_warmpool_size=a.replicas_per_image,
        warm_per_task=True,
        placement=getattr(a, "placement", "round-robin"),
        warm_create_budget=max(0, int(wcb)),
        overcommit_factor=max(4.0, a.overcommit_factor),
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )
    bench_core.apply_claim_mode_kwargs(cfg_kwargs, a)
    cfg = FleetConfig(**cfg_kwargs)

    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(all_tasks))
    mif = "n/a"
    if getattr(cfg, "batch_claims", None):
        mif = getattr(cfg.batch_claims, "max_in_flight", "default(20)")
    print(f"[mc-warm-burst] clusters={n_clusters} mode={getattr(a, 'claim_mode', 'per_claim')}(mif={mif}) "
          f"standing={len(images)}x{a.replicas_per_image}={total_warm} "
          f"concs={concs} verify_exec={a.verify_exec}", flush=True)

    points = []
    verify_out = None
    reap_info = None
    setup_t0 = time.monotonic()
    try:
        await af.setup()
        w_init = await burst_mod._wait_min_ready(af, total_warm, timeout=1200.0, require_zero_terminating=True)
        setup_s = round(time.monotonic() - setup_t0, 2)
        print(f"  SETUP {total_warm} warm sandboxes across {n_clusters} clusters in {setup_s}s "
              f"(ready={w_init['ready']})", flush=True)

        for c in concs:
            for rep_idx in range(a.repeats):
                w_info = await burst_mod._wait_min_ready(af, total_warm, timeout=900.0, require_zero_terminating=True)
                if a.post_warm_settle_s > 0:
                    await asyncio.sleep(a.post_warm_settle_s)
                proc_before = burst_mod.detailed_proc_stats()
                burst_tasks = all_tasks[:c]
                exec_sem = asyncio.Semaphore(max(1, a.exec_concurrency))

                t_burst = time.monotonic()
                outcomes = await asyncio.gather(*(
                    burst_mod._acquire_and_probe(af, t, t_burst, a.verify_exec, a.probe, exec_sem)
                    for t in burst_tasks
                ))
                total_wall_s = time.monotonic() - t_burst
                proc_held = burst_mod.detailed_proc_stats()

                ok_rows = [o for o in outcomes if o["ok"]]
                err_rows = [o for o in outcomes if not o["ok"]]
                per_cluster = collections.Counter(
                    o["handle"].cluster_name for o in ok_rows if o["handle"] is not None
                )
                ready_offsets = [o["ready_offset_s"] for o in ok_rows if o["ready_offset_s"] is not None]
                claim_wall_s = max(ready_offsets) if ready_offsets else total_wall_s

                wire_ok = [o["wire_claim_s"] for o in ok_rows]
                cmd_ok = [o["first_cmd_exec_s"] for o in ok_rows if o["first_cmd_exec_s"] is not None]
                ttfi_wall = [o["ttfi_wall_s"] for o in ok_rows if o["ttfi_wall_s"] is not None]
                ttfi_inline = [o["ttfi_inline_s"] for o in ok_rows if o["ttfi_inline_s"] is not None]
                probe_errs = [o["probe_err"] for o in ok_rows if o["probe_err"] is not None]
                errs = [o["err"] for o in err_rows if o["err"] is not None]

                t_rel = time.monotonic()
                await af.release_all()
                release_wall_s = time.monotonic() - t_rel
                proc_after = burst_mod.detailed_proc_stats()

                batch_mif = None
                if getattr(cfg, "batch_claims", None):
                    batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)

                fleet_claims_per_s = round(len(wire_ok) / claim_wall_s, 2) if claim_wall_s > 0 else None
                fleet_rel_per_s = round(len(wire_ok) / release_wall_s, 2) if release_wall_s > 0 else None

                pt = {
                    "concurrency": c,
                    "repeat": rep_idx,
                    "clusters": n_clusters,
                    "per_cluster_claims": dict(sorted(per_cluster.items())),
                    "claim_mode": getattr(a, "claim_mode", "per_claim"),
                    "batch_max_in_flight": batch_mif,
                    "verify_exec": a.verify_exec,
                    "warm_wait": w_info,
                    "burst_wall_s": round(claim_wall_s, 3),
                    "burst_and_exec_wall_s": round(total_wall_s, 3),
                    "claims_per_s": fleet_claims_per_s,
                    "per_cluster_claims_per_s": round(fleet_claims_per_s / n_clusters, 2) if fleet_claims_per_s is not None else None,
                    "release_wall_s": round(release_wall_s, 3),
                    "release_sandboxes_per_s": fleet_rel_per_s,
                    "per_cluster_release_sandboxes_per_s": round(fleet_rel_per_s / n_clusters, 2) if fleet_rel_per_s is not None else None,
                    "ok": len(wire_ok),
                    "err": len(errs),
                    "probe_err": len(probe_errs),
                    "failure_rate_pct": round(100.0 * (len(errs) + len(probe_errs)) / c, 5),
                    "wire_claim_s": bench_core.quantiles(wire_ok),
                    "first_cmd_exec_s": bench_core.quantiles(cmd_ok),
                    "ttfi_s": bench_core.quantiles(ttfi_wall),
                    "ttfi_inline_s": bench_core.quantiles(ttfi_inline),
                    "proc_before": proc_before,
                    "proc_held": proc_held,
                    "proc_after": proc_after,
                    "errors_sample": (errs + probe_errs)[:8],
                }
                points.append(pt)
                print(
                    f"  MC-BURST c={c} rep={rep_idx}: claim_wall={pt['burst_wall_s']}s "
                    f"({pt['claims_per_s']}/s fleet, {pt['per_cluster_claims_per_s']}/s/cluster) "
                    f"rel={pt['release_wall_s']}s ({pt['release_sandboxes_per_s']}/s fleet, "
                    f"{pt['per_cluster_release_sandboxes_per_s']}/s/cluster) "
                    f"ttfi(first={pt['ttfi_s']['first']} p50={pt['ttfi_s']['p50']} "
                    f"avg={pt['ttfi_s']['avg']} p99={pt['ttfi_s']['p99']} max={pt['ttfi_s']['max']}) "
                    f"ok={pt['ok']}/{c} probe_err={pt['probe_err']} "
                    f"held_socks={proc_held['open_sockets']} dist={dict(sorted(per_cluster.items()))}",
                    flush=True,
                )
        await af.teardown()
        verify_out = await bench_core.safe_verify_teardown(af)
        reap_info = await bench_core.wait_clusters_drained(clusters, timeout=300.0, settle_s=0.0)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception as td_exc:
            print(f"[mc-warm-burst] cleanup warning after failure: {td_exc}", flush=True)
        raise
    finally:
        bench_core.close_fleet(af, wait=False)

    return {
        "suite": "warm-burst",
        "clusters": n_clusters,
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "standing_total": total_warm,
        "setup_s": setup_s,
        "points": points,
        "verify_teardown": verify_out,
        "reap_zero_pods": reap_info,
    }


async def _run_golden_small_multijob(a, images: list[str], sel, rc) -> dict:
    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    n_clusters = max(1, len(clusters))
    n_jobs = a.jobs
    batch_problems = a.job_problems
    rollouts_per_prob = a.job_rollouts
    per_job_conc = batch_problems * rollouts_per_prob
    total_conc = n_jobs * per_job_conc

    # Partition / stride across the images deterministically across the jobs
    job_task_lists: list[list[Task]] = []
    all_union_tasks: list[Task] = []
    for j in range(n_jobs):
        offset = (j * 61) % len(images)
        j_imgs = [images[(offset + k) % len(images)] for k in range(batch_problems)]
        j_tasks = [
            Task(id=f"job{j}-{bench_core._sid(img)}-r{g}", image=img)
            for g in range(rollouts_per_prob)
            for img in j_imgs
        ]
        job_task_lists.append(j_tasks)
        all_union_tasks.extend(j_tasks)

    img_demand = collections.Counter(t.image for t in all_union_tasks)
    max_wp = max(img_demand.values())

    wcb = getattr(a, "warm_create_budget", 2500)
    prov_cfg_kwargs = dict(
        clusters=clusters,
        max_concurrent=total_conc,
        max_warmpool_size=max_wp,
        warm_per_task=True,
        placement=getattr(a, "placement", "round-robin"),
        warm_create_budget=max(0, int(wcb)),
        overcommit_factor=4.0,
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )
    # Provisioner fleet only creates/tears down shared WarmPools (never claims sandboxes),
    # so keep default claim_mode="per_claim" to avoid opening 5 unused batch watches/Leases.
    prov_cfg = FleetConfig(**prov_cfg_kwargs)
    prov_fleet = AsyncSandboxFleet(prov_cfg)
    prov_fleet.load_tasks(ListSource(all_union_tasks))

    print(f"[golden-small] clusters={n_clusters} mode={getattr(a, 'claim_mode', 'per_claim')} "
          f"jobs={n_jobs} x ({batch_problems}x{rollouts_per_prob}={per_job_conc}) = {total_conc} concurrent "
          f"(unique_images={len(img_demand)} max_wp={max_wp} stagger={a.job_stagger_s}s)", flush=True)

    setup_t0 = time.monotonic()
    job_fleets: list[AsyncSandboxFleet] = []
    exec_sem = asyncio.Semaphore(max(1, a.exec_concurrency))
    verify_out = None
    reap_info = None
    try:
        await prov_fleet.setup()
        w_init = await burst_mod._wait_min_ready(prov_fleet, total_conc, timeout=1200.0, require_zero_terminating=True)
        setup_s = round(time.monotonic() - setup_t0, 2)
        if a.post_warm_settle_s > 0:
            await asyncio.sleep(a.post_warm_settle_s)
        print(f"  PROVISIONED {w_init['ready']}/{total_conc} warm sandboxes in {setup_s}s", flush=True)

        for j in range(n_jobs):
            j_cfg_kwargs = dict(
                clusters=bench_core.build_clusters(a, sel=sel, rc=rc),
                max_concurrent=per_job_conc,
                adopt_existing=True,
                overcommit_factor=4.0,
                template=bench_core.build_template_spec(a, sel=sel, rc=rc),
            )
            bench_core.apply_claim_mode_kwargs(j_cfg_kwargs, a)
            j_cfg = FleetConfig(**j_cfg_kwargs)
            jf = AsyncSandboxFleet(j_cfg)
            jf.load_tasks(ListSource(job_task_lists[j]))
            await jf.setup()
            job_fleets.append(jf)

        proc_before = burst_mod.detailed_proc_stats()
        hold_ready_barrier = asyncio.Event()
        jobs_at_barrier = 0
        barrier_lock = asyncio.Lock()
        proc_peak_held: dict = {}

        async def _run_one_job(j_idx: int, jf: AsyncSandboxFleet, j_tasks: list[Task]) -> dict:
            nonlocal jobs_at_barrier, proc_peak_held
            if a.job_stagger_s > 0 and j_idx > 0:
                await asyncio.sleep(j_idx * a.job_stagger_s)
            jt0 = time.monotonic()
            outcomes = await asyncio.gather(*(
                burst_mod._acquire_and_probe(jf, t, jt0, True, a.probe, exec_sem)
                for t in j_tasks
            ))
            step_barrier_s = time.monotonic() - jt0

            async with barrier_lock:
                jobs_at_barrier += 1
                if jobs_at_barrier >= n_jobs:
                    proc_peak_held = burst_mod.detailed_proc_stats()
                    hold_ready_barrier.set()
            barrier_timed_out = False
            try:
                await asyncio.wait_for(hold_ready_barrier.wait(), timeout=180.0)
            except asyncio.TimeoutError:
                barrier_timed_out = True
                print(f"  WARNING [golden-small] job {j_idx} timed out waiting 180s at hold barrier", flush=True)

            t_rel = time.monotonic()
            await jf.release_all()
            rel_s = time.monotonic() - t_rel

            ok_rows = [o for o in outcomes if o["ok"]]
            err_rows = [o for o in outcomes if not o["ok"]]
            wire_ok = [o["wire_claim_s"] for o in ok_rows]
            ttfi_wall = [o["ttfi_wall_s"] for o in ok_rows if o["ttfi_wall_s"] is not None]
            ttfi_inline = [o["ttfi_inline_s"] for o in ok_rows if o["ttfi_inline_s"] is not None]
            probe_errs = [o["probe_err"] for o in ok_rows if o["probe_err"] is not None]
            errs = [o["err"] for o in err_rows if o["err"] is not None]

            return {
                "job_id": j_idx,
                "tasks": len(j_tasks),
                "ok": len(ok_rows) - len(probe_errs),
                "claim_err": len(errs),
                "probe_err": len(probe_errs),
                "barrier_timed_out": barrier_timed_out,
                "step_barrier_s": round(step_barrier_s, 3),
                "release_wall_s": round(rel_s, 3),
                "wire_claim_s": bench_core.quantiles(wire_ok),
                "ttfi_s": bench_core.quantiles(ttfi_wall),
                "ttfi_inline_s": bench_core.quantiles(ttfi_inline),
                "errors_sample": (errs + probe_errs)[:4],
                "_raw_wire": wire_ok,
                "_raw_ttfi": ttfi_wall,
                "_raw_ttfi_inline": ttfi_inline,
            }

        t_all = time.monotonic()
        job_results = await asyncio.gather(*(
            _run_one_job(j, job_fleets[j], job_task_lists[j]) for j in range(n_jobs)
        ))
        all_jobs_wall_s = round(time.monotonic() - t_all, 3)

        for jf in job_fleets:
            await jf.teardown()
            bench_core.close_fleet(jf, wait=False)
        job_fleets.clear()

        proc_after = burst_mod.detailed_proc_stats()
        await prov_fleet.teardown()
        verify_out = await bench_core.safe_verify_teardown(prov_fleet)
        reap_info = await bench_core.wait_clusters_drained(clusters, timeout=300.0, settle_s=0.0)
    except BaseException:
        for jf in job_fleets:
            try:
                await jf.release_all()
                await jf.teardown()
            except Exception as jf_exc:
                print(f"[golden-small] job fleet cleanup warning: {jf_exc}", flush=True)
            finally:
                bench_core.close_fleet(jf, wait=False)
        try:
            await prov_fleet.release_all()
            await prov_fleet.teardown()
        except Exception as pf_exc:
            print(f"[golden-small] prov_fleet cleanup warning: {pf_exc}", flush=True)
        raise
    finally:
        bench_core.close_fleet(prov_fleet, wait=False)

    all_wire = [x for jr in job_results for x in jr.pop("_raw_wire", [])]
    all_ttfi = [x for jr in job_results for x in jr.pop("_raw_ttfi", [])]
    all_ttfi_inline = [x for jr in job_results for x in jr.pop("_raw_ttfi_inline", [])]
    tot_ok = sum(jr["ok"] for jr in job_results)
    tot_err = sum(jr["claim_err"] + jr["probe_err"] for jr in job_results)
    barriers = [jr["step_barrier_s"] for jr in job_results]
    agg_tps = round(tot_ok / all_jobs_wall_s, 2) if all_jobs_wall_s > 0 else None

    print(
        f"RESULT GOLDEN-SMALL mode={getattr(a, 'claim_mode', 'per_claim')}: wall={all_jobs_wall_s}s "
        f"ok={tot_ok}/{total_conc} err={tot_err} "
        f"({round(100.0 * tot_err / total_conc, 5)}%) "
        f"job_barrier(avg={round(sum(barriers)/len(barriers),2)}s max={max(barriers)}s) "
        f"ttfi(first={bench_core.quantiles(all_ttfi)['first']} "
        f"p50={bench_core.quantiles(all_ttfi)['p50']} "
        f"p99={bench_core.quantiles(all_ttfi)['p99']} "
        f"max={bench_core.quantiles(all_ttfi)['max']}) "
        f"peak_socks={proc_peak_held.get('open_sockets')}",
        flush=True,
    )

    return {
        "suite": "golden-small",
        "clusters": n_clusters,
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "jobs": n_jobs,
        "per_job_sandboxes": per_job_conc,
        "total_concurrent_sandboxes": total_conc,
        "per_cluster_concurrent_sandboxes": round(total_conc / n_clusters, 2),
        "unique_images_used": len(img_demand),
        "setup_s": setup_s,
        "all_jobs_wall_s": all_jobs_wall_s,
        "aggregate_tasks_per_s": agg_tps,
        "per_cluster_tasks_per_s": round(agg_tps / n_clusters, 2) if agg_tps is not None else None,
        "total_ok": tot_ok,
        "total_err": tot_err,
        "failure_rate_pct": round(100.0 * tot_err / total_conc, 5),
        "job_step_barrier_s": bench_core.quantiles(barriers),
        "aggregate_wire_claim_s": bench_core.quantiles(all_wire),
        "aggregate_ttfi_s": bench_core.quantiles(all_ttfi),
        "aggregate_ttfi_inline_s": bench_core.quantiles(all_ttfi_inline),
        "proc_before": proc_before,
        "proc_peak_held": proc_peak_held,
        "proc_after": proc_after,
        "per_job": job_results,
        "verify_teardown": verify_out,
        "reap_zero_pods": reap_info,
    }


async def _main(a):
    if not a.contexts and not a.context:
        raise SystemExit("--contexts ctx1,ctx2,... is required")
    images = [l.strip() for l in open(a.images_file) if l.strip()][:a.problems]
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None

    if a.suite == "warm-burst":
        res = await _run_multicluster_warm_burst(a, images, sel, rc)
    else:
        res = await _run_golden_small_multijob(a, images, sel, rc)

    out = {"params": vars(a), "result": res}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"-> {a.out}", flush=True)


def main():
    p = bench_core.build_parser(
        "Multi-cluster SandboxBatch fleet benchmark",
        namespace="agent-sandbox-rl",
        problems=500,
        cpu="125m",
        memory="256Mi",
        tmp_disk_gi=4,
        placement="round-robin",
        probe="python3 -c 'import os; assert os.path.isdir(\"/testbed\"); print(1)'",
        termination_grace_seconds=0,
        warm_create_budget=2500,
        out="results/multicluster.json",
    )
    p.add_argument("--suite", choices=["warm-burst", "golden-small"], required=True)
    p.add_argument("--replicas-per-image", type=int, default=8)
    p.add_argument("--concurrencies", default="1000,2000,4000")
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--post-warm-settle-s", type=float, default=5.0)
    p.add_argument("--verify-exec", action="store_true")
    p.add_argument("--exec-concurrency", type=int, default=1024)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--job-problems", type=int, default=128)
    p.add_argument("--job-rollouts", type=int, default=4)
    p.add_argument("--job-stagger-s", type=float, default=0.5)
    a = p.parse_args()
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
