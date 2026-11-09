#!/usr/bin/env python3
"""Standing-fleet claim, single-clock TTFI burst, & multi-wave soak benchmark (`run_claim_soak_bench.py`).

Supports two modes:
  1. `--mode burst` (default):
     Measures true single-clock per-sandbox TTFI (`t_first_cmd_done - t_wire_claim_start`)
     inline inside each sandbox's coroutine when `--verify-exec` is set, alongside
     `wire_claim_s`, `first_cmd_exec_s`, client open socket counts, and bulk release rate.
     Requires BOTH `ready >= min_ready` AND `0` Terminating workload pods before firing each burst.
  2. `--mode soak`:
     Executes `total_claims` (`--total-claims`, e.g. `20,000`) claim + release cycles in
     waves of `--wave-size` from a single long-lived Python process to verify zero FD/socket
     leaks and eliminate the legacy ~1,500–2,500 single-process watch ceiling.
"""
from __future__ import annotations
import asyncio, json, os, time

import bench_core
from agent_sandbox_rl import (AsyncSandboxFleet, FleetConfig, Task,
                              constants as _rl_constants)
from agent_sandbox_rl.sources import ListSource


_TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "0A": "LISTEN",
}


def detailed_proc_stats() -> dict:
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
                next(fh, None)
                for line in fh:
                    parts = line.split()
                    if len(parts) >= 10 and parts[9] in my_inodes:
                        st = _TCP_STATES.get(parts[3].upper(), f"0x{parts[3]}")
                        states[st] = states.get(st, 0) + 1
        except OSError:
            pass
    base["tcp_states"] = states
    return base


def _total_ready_warm_and_terminating(af: AsyncSandboxFleet) -> tuple[int, int, int]:
    ready_sum = 0
    spec_sum = 0
    terminating_pods = 0
    sel = af._fleet.run_selector()
    for c in af.registry:
        wps = c.resources._list_objects(_rl_constants.WARMPOOLS_PLURAL, sel)
        for wp in wps:
            spec_sum += int((wp.get("spec") or {}).get("replicas") or 0)
            ready_sum += int((wp.get("status") or {}).get("readyReplicas") or 0)
        pods = c.core_api.list_namespaced_pod(c.namespace).items
        for p in pods:
            if p.metadata.name.startswith("asrl-"):
                continue
            if p.metadata.deletion_timestamp is not None:
                terminating_pods += 1
    return ready_sum, spec_sum, terminating_pods


async def _wait_min_ready(
    af: AsyncSandboxFleet,
    min_ready: int,
    timeout: float = 600.0,
    require_zero_terminating: bool = True,
    fail_on_timeout: bool = True,
) -> dict:
    t0 = time.monotonic()
    deadline = t0 + timeout
    last_ready, last_spec, last_term = 0, 0, 0
    while time.monotonic() < deadline:
        last_ready, last_spec, last_term = await af._to_thread(_total_ready_warm_and_terminating, af)
        if last_ready >= min_ready and (not require_zero_terminating or last_term == 0):
            return {
                "ready": last_ready,
                "desired": last_spec,
                "terminating_pods": last_term,
                "min_ready_target": min_ready,
                "wait_s": round(time.monotonic() - t0, 2),
                "timed_out": False,
            }
        await asyncio.sleep(1.5)
    wait_s = round(time.monotonic() - t0, 2)
    if fail_on_timeout:
        raise TimeoutError(
            f"warm pool readiness timed out after {wait_s}s: ready={last_ready}/{min_ready} "
            f"(desired={last_spec}, terminating_pods={last_term})"
        )
    return {
        "ready": last_ready,
        "desired": last_spec,
        "terminating_pods": last_term,
        "min_ready_target": min_ready,
        "wait_s": wait_s,
        "timed_out": True,
    }


async def _acquire_and_probe(
    af: AsyncSandboxFleet,
    task: Task,
    t_burst: float,
    verify_exec: bool,
    probe: str,
    exec_sem: asyncio.Semaphore,
):
    t_wire0 = time.monotonic()
    try:
        h = await af.acquire(task)
        t_ready = time.monotonic()
    except Exception as exc:
        return {
            "handle": None,
            "ok": False,
            "wire_claim_s": time.monotonic() - t_wire0,
            "ready_offset_s": None,
            "exec_sem_wait_s": None,
            "first_cmd_exec_s": None,
            "ttfi_inline_s": None,
            "ttfi_wall_s": None,
            "probe_err": None,
            "err": f"{type(exc).__name__}: {exc}",
        }

    wire_s = t_ready - t_wire0
    ready_offset_s = t_ready - t_burst
    if not verify_exec:
        return {
            "handle": h,
            "ok": True,
            "wire_claim_s": wire_s,
            "ready_offset_s": ready_offset_s,
            "exec_sem_wait_s": 0.0,
            "first_cmd_exec_s": None,
            "ttfi_inline_s": None,
            "ttfi_wall_s": None,
            "probe_err": None,
            "err": None,
        }

    async with exec_sem:
        t_cmd0 = time.monotonic()
        sem_wait_s = t_cmd0 - t_ready
        try:
            out = await af._to_thread(
                h.exec,
                ["bash", "-lc", f"(\n{probe}\n) && printf '\\n__ASRL_OK__\\n'"],
                timeout=60.0,
            )
            t_cmd1 = time.monotonic()
            raw_out = out if isinstance(out, str) else (getattr(out, "stdout", "") or "")
            if "__ASRL_OK__" not in raw_out:
                raise RuntimeError(f"probe non-zero exit: {raw_out.strip()[:200]!r}")
            cmd_s = t_cmd1 - t_cmd0
            return {
                "handle": h,
                "ok": True,
                "wire_claim_s": wire_s,
                "ready_offset_s": ready_offset_s,
                "exec_sem_wait_s": sem_wait_s,
                "first_cmd_exec_s": cmd_s,
                "ttfi_inline_s": wire_s + cmd_s,
                "ttfi_wall_s": t_cmd1 - t_wire0,
                "probe_err": None,
                "err": None,
            }
        except Exception as pexc:
            return {
                "handle": h,
                "ok": True,
                "wire_claim_s": wire_s,
                "ready_offset_s": ready_offset_s,
                "exec_sem_wait_s": sem_wait_s,
                "first_cmd_exec_s": None,
                "ttfi_inline_s": None,
                "ttfi_wall_s": None,
                "probe_err": f"{type(pexc).__name__}: {pexc}",
                "err": None,
            }


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

    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    n_clusters = max(1, len(clusters))
    wcb = getattr(a, "warm_create_budget", 500)

    cfg_kwargs = dict(
        clusters=clusters,
        max_concurrent=max_c,
        max_warmpool_size=a.replicas_per_image,
        warm_per_task=True,
        placement=a.placement,
        warm_create_budget=max(0, int(wcb)),
        overcommit_factor=a.overcommit_factor,
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )
    bench_core.apply_claim_mode_kwargs(cfg_kwargs, a)
    cfg = FleetConfig(**cfg_kwargs)

    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(all_tasks))
    mif = "n/a"
    if getattr(cfg, "batch_claims", None):
        mif = getattr(cfg.batch_claims, "max_in_flight", "default(20)")
    target_ready = max(max_c, int(round(total_warm * a.min_ready_fraction)))
    print(f"[burst] mode={getattr(a, 'claim_mode', 'per_claim')}(mif={mif}) standing={len(images)}x{a.replicas_per_image}={total_warm} "
          f"target_ready={target_ready} verify_exec={a.verify_exec} probe={a.probe!r} concurrencies={concs}", flush=True)

    points = []
    verify_out = None
    setup_t0 = time.monotonic()
    try:
        await af.setup()
        setup_s = round(time.monotonic() - setup_t0, 2)
        print(f"  SETUP standing warm pools ready in {setup_s}s", flush=True)

        for c in concs:
            for rep_idx in range(a.repeats):
                w_info = await _wait_min_ready(af, target_ready, timeout=600.0, require_zero_terminating=True)
                if a.post_warm_settle_s > 0:
                    await asyncio.sleep(a.post_warm_settle_s)
                proc_before = detailed_proc_stats()
                burst_tasks = all_tasks[:c]
                exec_sem = asyncio.Semaphore(max(1, a.exec_concurrency))

                t_burst = time.monotonic()
                outcomes = await asyncio.gather(*(
                    _acquire_and_probe(af, t, t_burst, a.verify_exec, a.probe, exec_sem)
                    for t in burst_tasks
                ))
                total_wall_s = time.monotonic() - t_burst
                proc_held = detailed_proc_stats()

                ok_rows = [o for o in outcomes if o["ok"]]
                err_rows = [o for o in outcomes if not o["ok"]]
                wire_ok = [o["wire_claim_s"] for o in ok_rows]
                wire_err = [o["wire_claim_s"] for o in err_rows]
                ready_offsets = [o["ready_offset_s"] for o in ok_rows if o["ready_offset_s"] is not None]
                burst_claims_wall_s = max(ready_offsets) if ready_offsets else total_wall_s

                cmd_ok = [o["first_cmd_exec_s"] for o in ok_rows if o["first_cmd_exec_s"] is not None]
                ttfi_inline = [o["ttfi_inline_s"] for o in ok_rows if o["ttfi_inline_s"] is not None]
                ttfi_wall = [o["ttfi_wall_s"] for o in ok_rows if o["ttfi_wall_s"] is not None]
                sem_waits = [o["exec_sem_wait_s"] for o in ok_rows if o["exec_sem_wait_s"] is not None]
                probe_errs = [o["probe_err"] for o in ok_rows if o["probe_err"] is not None]
                errs = [o["err"] for o in err_rows if o["err"] is not None]

                t_rel = time.monotonic()
                await af.release_all()
                release_wall_s = time.monotonic() - t_rel
                proc_after = detailed_proc_stats()

                batch_mif = None
                if getattr(cfg, "batch_claims", None):
                    batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)

                claims_rate = round(len(wire_ok) / burst_claims_wall_s, 2) if burst_claims_wall_s > 0 else None
                rel_rate = round(len(wire_ok) / release_wall_s, 2) if release_wall_s > 0 else None
                pt = {
                    "concurrency": c,
                    "repeat": rep_idx,
                    "clusters": n_clusters,
                    "claim_mode": getattr(a, "claim_mode", "per_claim"),
                    "batch_max_in_flight": batch_mif,
                    "verify_exec": a.verify_exec,
                    "warm_wait": w_info,
                    "burst_wall_s": round(burst_claims_wall_s, 3),
                    "burst_and_exec_wall_s": round(total_wall_s, 3),
                    "claims_per_s": claims_rate,
                    "per_cluster_claims_per_s": round(claims_rate / n_clusters, 2) if claims_rate is not None else None,
                    "ttfi_goodput_per_s": round(len(ttfi_wall) / total_wall_s, 2) if total_wall_s > 0 and ttfi_wall else None,
                    "release_wall_s": round(release_wall_s, 3),
                    "release_sandboxes_per_s": rel_rate,
                    "per_cluster_release_sandboxes_per_s": round(rel_rate / n_clusters, 2) if rel_rate is not None else None,
                    "ok": len(wire_ok),
                    "err": len(errs),
                    "probe_err": len(probe_errs),
                    "failure_rate_pct": round(100.0 * (len(errs) + len(probe_errs)) / c, 5) if c > 0 else None,
                    "latency_s": bench_core.quantiles(wire_ok),
                    "latency_err_s": bench_core.quantiles(wire_err),
                    "first_cmd_exec_s": bench_core.quantiles(cmd_ok),
                    "exec_sem_wait_s": bench_core.quantiles(sem_waits),
                    "ttfi_s": bench_core.quantiles(ttfi_wall),
                    "ttfi_inline_s": bench_core.quantiles(ttfi_inline),
                    "proc_before": proc_before,
                    "proc_held": proc_held,
                    "proc_after": proc_after,
                    "errors_sample": (errs + probe_errs)[:5],
                }
                points.append(pt)
                print(
                    f"  BURST c={c} rep={rep_idx}: claim_wall={pt['burst_wall_s']}s "
                    f"({pt['claims_per_s']}/s) claim_p50={pt['latency_s']['p50']}s "
                    f"claim_max={pt['latency_s']['max']}s "
                    f"ttfi(first={pt['ttfi_s']['first']} p50={pt['ttfi_s']['p50']} "
                    f"avg={pt['ttfi_s']['avg']} p99={pt['ttfi_s']['p99']} max={pt['ttfi_s']['max']}) "
                    f"rel={pt['release_wall_s']}s ({pt['release_sandboxes_per_s']}/s) "
                    f"ok={pt['ok']}/{c} probe_err={pt['probe_err']} "
                    f"held_socks={proc_held['open_sockets']}",
                    flush=True,
                )
        await af.teardown()
        verify_out = await bench_core.safe_verify_teardown(af)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception as td_exc:
            print(f"[burst] cleanup warning after failure: {td_exc}", flush=True)
        raise
    finally:
        bench_core.close_fleet(af, wait=False)

    batch_mif = None
    if getattr(cfg, "batch_claims", None):
        batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)

    return {
        "mode": "burst",
        "clusters": n_clusters,
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "batch_max_in_flight": batch_mif,
        "verify_exec": a.verify_exec,
        "probe": a.probe,
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
    wave_size = a.wave_size
    total_warm = len(images) * a.replicas_per_image
    if wave_size > total_warm:
        raise SystemExit(
            f"wave_size {wave_size} exceeds standing warm capacity "
            f"{len(images)} x {a.replicas_per_image} = {total_warm}")

    all_tasks = [
        Task(id=f"{bench_core._sid(img)}-r{r}", image=img)
        for r in range(a.replicas_per_image)
        for img in images
    ]

    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    n_clusters = max(1, len(clusters))
    wcb = getattr(a, "warm_create_budget", 500)
    cfg_kwargs = dict(
        clusters=clusters,
        max_concurrent=wave_size,
        max_warmpool_size=a.replicas_per_image,
        warm_per_task=True,
        placement=a.placement,
        warm_create_budget=max(0, int(wcb)),
        overcommit_factor=a.overcommit_factor,
        template=bench_core.build_template_spec(a, sel=sel, rc=rc),
    )
    bench_core.apply_claim_mode_kwargs(cfg_kwargs, a)
    cfg = FleetConfig(**cfg_kwargs)

    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(all_tasks))
    mif = "n/a"
    if getattr(cfg, "batch_claims", None):
        mif = getattr(cfg.batch_claims, "max_in_flight", "default(20)")
    target_ready = max(wave_size, int(round(total_warm * a.min_ready_fraction)))
    print(f"[soak] mode={getattr(a, 'claim_mode', 'per_claim')}(mif={mif}) "
          f"total_claims={a.total_claims} wave_size={wave_size} "
          f"standing={len(images)}x{a.replicas_per_image}={total_warm}", flush=True)

    waves = []
    all_ok_lats: list[float] = []
    all_err_lats: list[float] = []
    all_errors: list[str] = []
    cum_claims = 0
    cum_ok = 0
    cum_err = 0
    verify_out = None
    proc_init = detailed_proc_stats()
    proc_end_before_td: dict = {}
    setup_t0 = time.monotonic()
    try:
        await af.setup()
        setup_s = round(time.monotonic() - setup_t0, 2)
        print(f"  SETUP standing warm pools ready in {setup_s}s", flush=True)

        soak_t0 = time.monotonic()
        wave_idx = 0
        exec_sem = asyncio.Semaphore(max(1, a.exec_concurrency))
        while cum_claims < a.total_claims:
            cur_n = min(wave_size, a.total_claims - cum_claims)
            w_info = await _wait_min_ready(
                af,
                min(target_ready, total_warm),
                timeout=300.0,
                require_zero_terminating=True,
            )
            if a.post_warm_settle_s > 0:
                await asyncio.sleep(a.post_warm_settle_s)
            offset = (wave_idx * cur_n) % len(all_tasks)
            w_tasks = [all_tasks[(offset + k) % len(all_tasks)] for k in range(cur_n)]

            tw0 = time.monotonic()
            outcomes = await asyncio.gather(*(
                _acquire_and_probe(af, t, tw0, a.verify_exec, a.probe, exec_sem)
                for t in w_tasks
            ))
            acq_wall = time.monotonic() - tw0

            ok_rows = [o for o in outcomes if o["ok"] and o["probe_err"] is None]
            err_rows = [o for o in outcomes if (not o["ok"]) or (o["probe_err"] is not None)]
            w_ok = [o["wire_claim_s"] for o in ok_rows]
            w_err = [o["wire_claim_s"] for o in err_rows]
            all_ok_lats.extend(w_ok)
            all_err_lats.extend(w_err)
            for o in err_rows:
                msg = o["err"] or o["probe_err"]
                if msg and len(all_errors) < 20:
                    all_errors.append(f"w{wave_idx}: {msg}")

            t_rel = time.monotonic()
            await af.release_all()
            rel_wall = time.monotonic() - t_rel

            cum_claims += cur_n
            cum_ok += len(ok_rows)
            cum_err += len(err_rows)
            ps = detailed_proc_stats()
            w_rate = round(len(ok_rows) / acq_wall, 2) if acq_wall > 0 else None
            w_rel_rate = round(len(ok_rows) / rel_wall, 2) if rel_wall > 0 else None
            rec = {
                "wave": wave_idx,
                "n": cur_n,
                "clusters": n_clusters,
                "cum_claims": cum_claims,
                "ok": len(ok_rows),
                "err": len(err_rows),
                "warm_wait": w_info,
                "acquire_wall_s": round(acq_wall, 3),
                "claims_per_s": w_rate,
                "per_cluster_claims_per_s": round(w_rate / n_clusters, 2) if w_rate is not None else None,
                "release_wall_s": round(rel_wall, 3),
                "release_sandboxes_per_s": w_rel_rate,
                "per_cluster_release_sandboxes_per_s": round(w_rel_rate / n_clusters, 2) if w_rel_rate is not None else None,
                "latency_s": bench_core.quantiles(w_ok),
                "proc": ps,
            }
            waves.append(rec)
            if wave_idx % 10 == 0 or cum_claims >= a.total_claims or len(err_rows) > 0:
                print(
                    f"  SOAK {cum_claims}/{a.total_claims}: acq={rec['acquire_wall_s']}s "
                    f"({rec['claims_per_s']}/s) p50={rec['latency_s']['p50']}s "
                    f"p99={rec['latency_s']['p99']}s rel={rec['release_wall_s']}s "
                    f"ok={cum_ok} err={cum_err} fds={ps['open_fds']} "
                    f"sockets={ps['open_sockets']} rss={ps['vm_rss_mb']}MB",
                    flush=True,
                )
            wave_idx += 1

        soak_wall_s = round(time.monotonic() - soak_t0, 2)
        proc_end_before_td = detailed_proc_stats()
        await af.teardown()
        verify_out = await bench_core.safe_verify_teardown(af)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception as td_exc:
            print(f"[soak] cleanup warning after failure: {td_exc}", flush=True)
        raise
    finally:
        proc_after_td = detailed_proc_stats()
        bench_core.close_fleet(af, wait=False)

    batch_mif = None
    if getattr(cfg, "batch_claims", None):
        batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)
    goodput = round(cum_ok / soak_wall_s, 2) if soak_wall_s > 0 else None

    return {
        "mode": "soak",
        "clusters": n_clusters,
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "batch_max_in_flight": batch_mif,
        "total_claims": cum_claims,
        "total_ok": cum_ok,
        "total_err": cum_err,
        "failure_rate_pct": round(100.0 * cum_err / cum_claims, 5) if cum_claims > 0 else None,
        "wave_size": wave_size,
        "standing_pools": len(images),
        "replicas_per_image": a.replicas_per_image,
        "min_ready_fraction": a.min_ready_fraction,
        "post_warm_settle_s": a.post_warm_settle_s,
        "setup_s": setup_s,
        "soak_wall_s": soak_wall_s,
        "goodput_claims_per_s": goodput,
        "per_cluster_goodput_claims_per_s": round(goodput / n_clusters, 2) if goodput is not None else None,
        "aggregate_latency_s": bench_core.quantiles(all_ok_lats),
        "aggregate_latency_err_s": bench_core.quantiles(all_err_lats),
        "proc_init": proc_init,
        "proc_end_before_teardown": proc_end_before_td,
        "proc_after_teardown": proc_after_td,
        "waves": waves,
        "verify_teardown": verify_out,
        "errors_sample": all_errors[:12],
    }


async def _main(a):
    if not a.in_cluster and not a.context and not a.contexts:
        raise SystemExit("one of --context, --contexts, or --in-cluster is required")
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
        "Standing-fleet claim, single-clock TTFI burst, & multi-wave soak benchmark",
        namespace="agent-sandbox-rl",
        problems=200,
        concurrency=1000,
        cpu="125m",
        memory="512Mi",
        probe="python3 -c 'print(1)'",
        termination_grace_seconds=0,
        warm_create_budget=500,
        out="results/claim_ttfi_burst.json",
    )
    p.add_argument("--mode", choices=["burst", "soak"], default="burst")
    p.add_argument("--replicas-per-image", type=int, default=5)
    p.add_argument("--concurrencies", default="100,250,500,1000")
    p.add_argument("--repeats", type=int, default=3)
    p.add_argument("--min-ready-fraction", type=float, default=1.0)
    p.add_argument("--post-warm-settle-s", type=float, default=5.0)
    p.add_argument("--verify-exec", action="store_true")
    p.add_argument("--exec-concurrency", type=int, default=1024,
                   help="max concurrent exec streams during inline TTFI (default 1024 >= c=1000)")
    p.add_argument("--total-claims", type=int, default=20000,
                   help="total claim+release cycles (--mode soak)")
    p.add_argument("--wave-size", type=int, default=50,
                   help="concurrent claims per wave (--mode soak)")
    a = p.parse_args()
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
