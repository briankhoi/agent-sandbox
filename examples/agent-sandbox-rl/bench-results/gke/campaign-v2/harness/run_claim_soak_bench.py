#!/usr/bin/env python3
"""Standing-fleet pure claim burst curve + 10k-claim endurance soak (Campaign 2).

Includes `PARITY_BENCHMARK_SPEC.md` fixes:
  * Uses `bench_core.build_template_spec()` so `--enable-cow`, `--unmanaged-network-policy`,
    and `--termination-grace-seconds` apply uniformly.
  * `--min-ready-fraction` (default `1.0` = `100%` warm-ready) and `--post-warm-settle-s`
    (default `5.0`) so burst tail latencies never hit unreplenished cold pods.
  * Reports succeeded-only `latency_s` strictly separated from failed-only `latency_err_s`.
"""
from __future__ import annotations
import asyncio, json, os, time

import bench_core
from agent_sandbox_rl import (AsyncSandboxFleet, ClusterConfig, FleetConfig,
                              Task, constants as _rl_constants)
from agent_sandbox_rl.sources import ListSource


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[idx], 4)


def _quantiles(vals: list[float]) -> dict:
    return {
        "n": len(vals),
        "avg": round(sum(vals) / len(vals), 4) if vals else None,
        "min": round(min(vals), 4) if vals else None,
        "p50": _pct(vals, 0.50),
        "p90": _pct(vals, 0.90),
        "p95": _pct(vals, 0.95),
        "p99": _pct(vals, 0.99),
        "max": round(max(vals), 4) if vals else None,
    }


_TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "0A": "LISTEN",
}


def detailed_proc_stats() -> dict:
    """Return bench_core.proc_stats() enriched with per-process TCP state counts."""
    base = bench_core.proc_stats()
    my_inodes: set[str] = set()
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                lnk = os.readlink(f"/proc/self/fd/{fd}")
                if lnk.startswith("socket:[") and lnk.endswith("]"):
                    my_inodes.add(lnk[8:-1])
            except OSError:
                pass
    except OSError:
        pass

    states: dict[str, int] = {}
    for net_path in ("/proc/self/net/tcp", "/proc/self/net/tcp6"):
        try:
            with open(net_path, encoding="utf-8") as fh:
                next(fh, None)  # header
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 10 and parts[9] in my_inodes:
                        st = _TCP_STATES.get(parts[3].upper(), f"0x{parts[3]}")
                        states[st] = states.get(st, 0) + 1
        except OSError:
            pass
    base["tcp_states"] = states
    return base


def _total_ready_warm(af: AsyncSandboxFleet) -> tuple[int, int]:
    """Return (ready_replicas, desired_replicas) across all warm pools in the namespace."""
    ready_sum = 0
    spec_sum = 0
    sel = af._fleet.run_selector()
    for c in af.registry:
        wps = c.resources._list_objects(_rl_constants.WARMPOOLS_PLURAL, sel)
        for wp in wps:
            spec_sum += int((wp.get("spec") or {}).get("replicas") or 0)
            ready_sum += int((wp.get("status") or {}).get("readyReplicas") or 0)
    return ready_sum, spec_sum


async def _wait_min_ready(af: AsyncSandboxFleet, min_ready: int, timeout: float = 600.0) -> dict:
    t0 = time.monotonic()
    deadline = t0 + timeout
    last_ready, last_spec = 0, 0
    while time.monotonic() < deadline:
        last_ready, last_spec = await af._to_thread(_total_ready_warm, af)
        if last_ready >= min_ready:
            return {
                "ready": last_ready,
                "desired": last_spec,
                "min_ready_target": min_ready,
                "wait_s": round(time.monotonic() - t0, 2),
                "timed_out": False,
            }
        await asyncio.sleep(1.5)
    return {
        "ready": last_ready,
        "desired": last_spec,
        "min_ready_target": min_ready,
        "wait_s": round(time.monotonic() - t0, 2),
        "timed_out": True,
    }


async def _acquire_timed(af: AsyncSandboxFleet, task: Task):
    t0 = time.monotonic()
    try:
        h = await af.acquire(task)
        return (h, time.monotonic() - t0, None)
    except Exception as exc:
        return (None, time.monotonic() - t0, f"{type(exc).__name__}: {exc}")


async def _run_burst(a, images: list[str], sel, rc) -> dict:
    concs = [int(x.strip()) for x in a.concurrencies.split(",") if x.strip()]
    max_c = max(concs)
    total_warm = len(images) * a.replicas_per_image
    if max_c > total_warm:
        raise SystemExit(
            f"max concurrency {max_c} exceeds standing warm capacity "
            f"{len(images)} x {a.replicas_per_image} = {total_warm}")

    all_tasks = [
        Task(id=f"{bench_core._sid(img)}-r{r}", image=img)
        for r in range(a.replicas_per_image)
        for img in images
    ]

    cluster = ClusterConfig(
        name="bench", context=a.context, in_cluster=a.in_cluster,
        namespace=a.namespace, node_selector=sel, runtime_class=rc)
    wcb = getattr(a, "warm_create_budget", 500)

    cfg = FleetConfig(
        clusters=[cluster],
        max_concurrent=max_c,
        max_warmpool_size=a.replicas_per_image,
        warm_per_task=True,
        placement="image-affinity",
        warm_create_budget=(wcb if wcb > 0 else None),
        overcommit_factor=a.overcommit_factor,
        claim_mode=a.claim_mode,
        batch_claims=bench_core.build_batch_config(a),
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )

    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(all_tasks))
    mif = cfg.batch_claims.max_in_flight or "default(20)"
    target_ready = max(max_c, int(round(total_warm * a.min_ready_fraction)))
    print(f"[burst] mode={a.claim_mode}(mif={mif}) standing={len(images)}x{a.replicas_per_image}={total_warm} "
          f"min_ready={target_ready} settle={a.post_warm_settle_s}s concurrencies={concs}", flush=True)

    points = []
    verify_out = None
    setup_t0 = time.monotonic()
    try:
        await af.setup()
        setup_s = round(time.monotonic() - setup_t0, 2)
        print(f"  SETUP standing warm pools ready in {setup_s}s", flush=True)

        for c in concs:
            for rep_idx in range(a.repeats):
                w_info = await _wait_min_ready(af, target_ready, timeout=600.0)
                if a.post_warm_settle_s > 0:
                    await asyncio.sleep(a.post_warm_settle_s)
                proc_before = detailed_proc_stats()
                burst_tasks = all_tasks[:c]

                t_burst = time.monotonic()
                outcomes = await asyncio.gather(*(_acquire_timed(af, t) for t in burst_tasks))
                burst_wall_s = time.monotonic() - t_burst
                proc_held = detailed_proc_stats()

                handles = [h for h, _, err in outcomes if h is not None and err is None]
                lats_ok = [lat for h, lat, err in outcomes if h is not None and err is None]
                lats_err = [lat for h, lat, err in outcomes if h is None or err is not None]
                errs = [err for _, _, err in outcomes if err is not None]

                probe_err = 0
                if a.verify_exec and handles:
                    sem = asyncio.Semaphore(min(256, len(handles)))

                    async def _exec_one(h):
                        async with sem:
                            return await af._to_thread(h.exec, ["bash", "-lc", a.probe], timeout=60.0)

                    ex_res = await asyncio.gather(*(_exec_one(h) for h in handles), return_exceptions=True)
                    probe_err = sum(1 for r in ex_res if isinstance(r, Exception))

                t_rel = time.monotonic()
                await af.release_all()
                release_wall_s = time.monotonic() - t_rel
                proc_after = detailed_proc_stats()

                pt = {
                    "concurrency": c,
                    "repeat": rep_idx,
                    "claim_mode": a.claim_mode,
                    "batch_max_in_flight": cfg.batch_claims.max_in_flight,
                    "warm_wait": w_info,
                    "burst_wall_s": round(burst_wall_s, 3),
                    "claims_per_s": round(len(lats_ok) / burst_wall_s, 2) if burst_wall_s > 0 else None,
                    "release_wall_s": round(release_wall_s, 3),
                    "ok": len(lats_ok),
                    "err": len(errs),
                    "probe_err": probe_err,
                    "latency_s": _quantiles(lats_ok),
                    "latency_err_s": _quantiles(lats_err),
                    "proc_before": proc_before,
                    "proc_held": proc_held,
                    "proc_after": proc_after,
                    "errors_sample": errs[:5],
                }
                points.append(pt)
                print(
                    f"  BURST c={c} rep={rep_idx}: wall={pt['burst_wall_s']}s "
                    f"rate={pt['claims_per_s']}/s p50={pt['latency_s']['p50']}s "
                    f"p95={pt['latency_s']['p95']}s p99={pt['latency_s']['p99']}s "
                    f"max={pt['latency_s']['max']}s rel={pt['release_wall_s']}s "
                    f"ok={pt['ok']}/{c} sockets_after={proc_after['open_sockets']} "
                    f"tcp={proc_after['tcp_states']}",
                    flush=True,
                )
        await af.teardown()
        try:
            verify_out = await af._to_thread(af._fleet.verify_teardown)
        except Exception as ve:
            verify_out = {"error": str(ve)}
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception:
            pass
        raise
    finally:
        af.close()

    return {
        "mode": "burst",
        "claim_mode": a.claim_mode,
        "batch_max_in_flight": cfg.batch_claims.max_in_flight,
        "standing_pools": len(images),
        "replicas_per_image": a.replicas_per_image,
        "standing_total": total_warm,
        "min_ready_fraction": a.min_ready_fraction,
        "post_warm_settle_s": a.post_warm_settle_s,
        "setup_s": setup_s,
        "points": points,
        "verify_teardown": verify_out,
    }


async def _run_soak(a, images: list[str], sel, rc) -> dict:
    wave_size = a.wave_size or len(images)
    total_warm = len(images) * a.replicas_per_image
    wave_template = [
        Task(id=f"{bench_core._sid(images[i % len(images)])}-w{i}", image=images[i % len(images)])
        for i in range(wave_size)
    ]
    plan_tasks = [
        Task(id=f"{bench_core._sid(img)}-p{r}", image=img)
        for r in range(a.replicas_per_image)
        for img in images
    ]

    cluster = ClusterConfig(
        name="bench", context=a.context, in_cluster=a.in_cluster,
        namespace=a.namespace, node_selector=sel, runtime_class=rc)
    wcb = getattr(a, "warm_create_budget", 400)

    cfg = FleetConfig(
        clusters=[cluster],
        max_concurrent=max(wave_size, a.concurrency),
        max_warmpool_size=a.replicas_per_image,
        warm_per_task=True,
        placement="image-affinity",
        warm_create_budget=(wcb if wcb > 0 else None),
        overcommit_factor=a.overcommit_factor,
        claim_mode=a.claim_mode,
        batch_claims=bench_core.build_batch_config(a),
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )

    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(plan_tasks))
    mif = cfg.batch_claims.max_in_flight or "default(20)"
    print(f"[soak] mode={a.claim_mode}(mif={mif}) total_claims={a.total_claims} "
          f"wave_size={wave_size} standing={len(images)}x{a.replicas_per_image}={total_warm}",
          flush=True)

    waves = []
    proc_init = detailed_proc_stats()
    verify_out = None
    setup_t0 = time.monotonic()
    soak_t0 = 0.0
    cum_claims = 0
    cum_ok = 0
    cum_err = 0
    all_errors: list[str] = []
    all_ok_lats: list[float] = []
    all_err_lats: list[float] = []
    try:
        await af.setup()
        setup_s = round(time.monotonic() - setup_t0, 2)
        await _wait_min_ready(af, total_warm, timeout=600.0)
        if a.post_warm_settle_s > 0:
            await asyncio.sleep(a.post_warm_settle_s)
        print(f"  SETUP standing warm pools ready in {setup_s}s; starting {a.total_claims}-claim soak",
              flush=True)

        soak_t0 = time.monotonic()
        wave_idx = 0
        while cum_claims < a.total_claims:
            n_this = min(wave_size, a.total_claims - cum_claims)
            # Wait until standing warm pools meet requested readiness (at least n_this * 1.5, or min_ready_fraction * total_warm)
            min_ready_needed = min(
                total_warm,
                max(int(round(total_warm * a.min_ready_fraction)), min(total_warm, int(n_this * 1.5))),
            )
            w_info = await _wait_min_ready(af, min_ready_needed, timeout=300.0)

            tasks_w = [
                Task(id=f"soak-w{wave_idx}-{j}", image=wave_template[j].image)
                for j in range(n_this)
            ]
            t_w = time.monotonic()
            outcomes = await asyncio.gather(*(_acquire_timed(af, t) for t in tasks_w))
            acq_wall_s = time.monotonic() - t_w

            lats_ok = [lat for h, lat, err in outcomes if h is not None and err is None]
            lats_err = [lat for h, lat, err in outcomes if h is None or err is not None]
            errs = [err for _, _, err in outcomes if err is not None]
            all_ok_lats.extend(lats_ok)
            all_err_lats.extend(lats_err)
            all_errors.extend(errs[:3])

            t_rel = time.monotonic()
            await af.release_all()
            rel_wall_s = time.monotonic() - t_rel

            cum_claims += n_this
            cum_ok += len(lats_ok)
            cum_err += len(errs)
            ps = detailed_proc_stats()

            rec = {
                "wave": wave_idx,
                "cumulative_claims": cum_claims,
                "wave_claims": n_this,
                "ok": len(lats_ok),
                "err": len(errs),
                "warm_wait": w_info,
                "acquire_wall_s": round(acq_wall_s, 3),
                "claims_per_s": round(len(lats_ok) / acq_wall_s, 2) if acq_wall_s > 0 else None,
                "release_wall_s": round(rel_wall_s, 3),
                "latency_s": _quantiles(lats_ok),
                "latency_err_s": _quantiles(lats_err),
                "proc": ps,
            }
            waves.append(rec)
            if wave_idx % 5 == 0 or cum_claims >= a.total_claims or errs:
                print(
                    f"  SOAK {cum_claims}/{a.total_claims}: acq={rec['acquire_wall_s']}s "
                    f"({rec['claims_per_s']}/s) p50={rec['latency_s']['p50']}s "
                    f"p99={rec['latency_s']['p99']}s rel={rec['release_wall_s']}s "
                    f"ok={cum_ok} err={cum_err} fds={ps['open_fds']} "
                    f"sockets={ps['open_sockets']} tcp={ps['tcp_states']} rss={ps['vm_rss_mb']}MB",
                    flush=True,
                )
            wave_idx += 1

        soak_wall_s = round(time.monotonic() - soak_t0, 2)
        proc_end_before_td = detailed_proc_stats()
        await af.teardown()
        try:
            verify_out = await af._to_thread(af._fleet.verify_teardown)
        except Exception as ve:
            verify_out = {"error": str(ve)}
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception:
            pass
        raise
    finally:
        af.close()

    return {
        "mode": "soak",
        "claim_mode": a.claim_mode,
        "batch_max_in_flight": cfg.batch_claims.max_in_flight,
        "total_claims": cum_claims,
        "total_ok": cum_ok,
        "total_err": cum_err,
        "wave_size": wave_size,
        "standing_pools": len(images),
        "replicas_per_image": a.replicas_per_image,
        "min_ready_fraction": a.min_ready_fraction,
        "post_warm_settle_s": a.post_warm_settle_s,
        "setup_s": setup_s,
        "soak_wall_s": soak_wall_s,
        "goodput_claims_per_s": round(cum_ok / soak_wall_s, 2) if soak_wall_s > 0 else None,
        "aggregate_latency_s": _quantiles(all_ok_lats),
        "aggregate_latency_err_s": _quantiles(all_err_lats),
        "proc_init": proc_init,
        "proc_end_before_teardown": proc_end_before_td,
        "proc_after_teardown": detailed_proc_stats(),
        "waves": waves,
        "verify_teardown": verify_out,
        "errors_sample": all_errors[:12],
    }


async def _main(a):
    if not a.in_cluster and not a.context:
        raise SystemExit("one of --context or --in-cluster is required")
    images = [l.strip() for l in open(a.images_file) if l.strip()][:a.problems]
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None

    if a.mode == "burst":
        res = await _run_burst(a, images, sel, rc)
    else:
        res = await _run_soak(a, images, sel, rc)

    out = {
        "params": {k: v for k, v in vars(a).items()},
        "result": res,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"-> {a.out}", flush=True)


def main():
    p = bench_core.build_parser(
        "Standing-fleet pure claim burst & 10k-claim soak benchmark",
        namespace="agent-sandbox-rl",
        problems=200,
        concurrency=1000,
        cpu="125m",
        memory="512Mi",
        probe="true",
        termination_grace_seconds=0,
        warm_create_budget=500,
        out="results/claim_soak.json",
    )
    p.add_argument("--mode", choices=["burst", "soak"], required=True)
    p.add_argument("--replicas-per-image", type=int, default=5,
                   help="standing warm replicas per image pool")
    p.add_argument("--concurrencies", default="100,200,500,1000",
                   help="comma-separated burst concurrencies (--mode burst)")
    p.add_argument("--repeats", type=int, default=3,
                   help="repeats per concurrency point (--mode burst)")
    p.add_argument("--min-ready-fraction", type=float, default=1.0,
                   help="fraction of standing warm sandboxes required Ready before firing (default 1.0)")
    p.add_argument("--post-warm-settle-s", type=float, default=5.0,
                   help="seconds to settle after min_ready reached before firing burst (default 5.0)")
    p.add_argument("--verify-exec", action="store_true",
                   help="also exec --probe on every claimed handle during burst")
    p.add_argument("--total-claims", type=int, default=10000,
                   help="total claim+release cycles (--mode soak)")
    p.add_argument("--wave-size", type=int, default=200,
                   help="concurrent claims per wave (--mode soak)")
    a = p.parse_args()
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
