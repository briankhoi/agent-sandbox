"""Shared benchmark engine for Campaign 2 (`sandbox-rl-performance` + `claim_mode`).

Includes all parity & telemetry fixes from `PARITY_BENCHMARK_SPEC.md`:
  * Shared `build_template_spec()` used across `bench_core`, `run_step_bench`, and
    `run_claim_soak_bench` so `--enable-cow`, `--unmanaged-network-policy`, and
    `--termination-grace-seconds` apply uniformly everywhere.
  * Uncapped `warm_create_budget` default (`1000`, matching `FleetConfig` & `RESULTS.md`
    `#10-#16`) and `window_size=auto` (`None`) unless explicitly overridden.
  * Succeeded-only `phases` (with `min/avg/p50/p90/p95/p99/max`) strictly separated from
    failed-only `phases_err` so fast-failing HTTP 429s never deflate claim latency.
  * Per-variant `/proc/self` socket, FD, and RSS telemetry (`proc_stats()`) + teardown
    verification (`verify_teardown()`).
"""
from __future__ import annotations
import argparse, asyncio, json, os, time

from agent_sandbox_rl import (AsyncSandboxFleet, BatchClaimConfig, ClusterConfig,
                              FleetConfig, ResourceSpec, TemplateSpec, Task)
try:
    from agent_sandbox_rl import GitRestoreReset
except ImportError:
    GitRestoreReset = None
from agent_sandbox_rl.sources import ListSource

_DEFAULT_STRATEGIES = "naive,sliding,pipelined"


def proc_stats() -> dict:
    """Snapshot process open FDs, socket FDs, and memory RSS/HWM from /proc/self."""
    open_fds = 0
    open_sockets = 0
    try:
        entries = os.listdir("/proc/self/fd")
        open_fds = len(entries)
        for fd in entries:
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
                if target.startswith("socket:["):
                    open_sockets += 1
            except OSError:
                pass
    except OSError:
        pass

    vm_rss_mb = None
    vm_hwm_mb = None
    threads = None
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    vm_rss_mb = round(int(line.split()[1]) / 1024.0, 2)
                elif line.startswith("VmHWM:"):
                    vm_hwm_mb = round(int(line.split()[1]) / 1024.0, 2)
                elif line.startswith("Threads:"):
                    threads = int(line.split()[1])
    except OSError:
        pass

    return {
        "open_fds": open_fds,
        "open_sockets": open_sockets,
        "vm_rss_mb": vm_rss_mb,
        "vm_hwm_mb": vm_hwm_mb,
        "threads": threads,
    }


def _sid(image: str) -> str:
    return image.rsplit("/", 1)[-1].replace("@", "-").replace(":", "-")[:60]


def _parse_selector(s):
    if not s or "=" not in s:
        return None
    k, v = s.split("=", 1)
    return {k: v}


def _variants(strategies: str, recycle: bool, recycle_only: bool = False):
    strats = [s.strip() for s in strategies.split(",") if s.strip()]
    if recycle_only:
        return [(s, True) for s in strats]
    out = [(s, False) for s in strats]
    if recycle:
        out += [(s, True) for s in strats]
    return out


def _pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[idx], 3)


def _phase_table(phases: dict) -> dict:
    t = {}
    for name, c in phases.items():
        n = c.get("count", 0) or 0
        total = c.get("total_s", 0.0) or 0.0
        entry = {
            "n": n,
            "avg": round(total / n, 4) if n else None,
            "total": round(total, 2),
            "max": c.get("max_s"),
        }
        for qk in ("min", "p50", "p90", "p95", "p99"):
            src = f"{qk}_s"
            if src in c:
                entry[qk] = c[src]
        t[name] = entry
    return t


def build_batch_config(a) -> BatchClaimConfig:
    mif = getattr(a, "batch_max_in_flight", 0) or None
    crps = getattr(a, "batch_create_rps", 0.0) or None
    return BatchClaimConfig(
        create_rps=crps,
        max_in_flight=mif,
        lease_duration=getattr(a, "batch_lease_duration", 300),
        work_budget=getattr(a, "batch_work_budget", 3600),
    )


def build_template_spec(a, *, sel, rc) -> TemplateSpec:
    """Shared TemplateSpec builder honoring --enable-cow, --unmanaged-network-policy, and --termination-grace-seconds."""
    cow_flag = getattr(a, "enable_cow", False)
    if isinstance(cow_flag, str):
        cow_flag = cow_flag.lower() in ("true", "1", "yes")

    extra_pod_spec: dict = {}
    if cow_flag:
        extra_pod_spec = {
            "containers": [{
                "name": "agent-runtime",
                "env": [
                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    {"name": "TMPDIR", "value": "/tmp"},
                ],
                "volumeMounts": [
                    {"name": "ram-scratch", "mountPath": "/tmp"},
                    {"name": "ram-scratch", "mountPath": "/root/.cache"},
                    {"name": "ram-scratch", "mountPath": "/tmp/pytest-of-root"},
                ],
            }],
            "volumes": [
                {"name": "ram-scratch", "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"}}
            ],
        }

    tgs = getattr(a, "termination_grace_seconds", -1)
    if tgs is not None and tgs >= 0:
        extra_pod_spec["terminationGracePeriodSeconds"] = tgs

    np_mode = "Unmanaged" if getattr(a, "unmanaged_network_policy", False) else None
    return TemplateSpec(
        runtime_class=rc,
        node_selector=sel,
        colocate_replicas=getattr(a, "colocate", False),
        resources=ResourceSpec(cpu=a.cpu, memory=a.memory),
        network_policy_management=np_mode,
        automount_service_account_token=False,
        extra_pod_spec=extra_pod_spec,
    )


def build_parser(description: str, **defaults) -> argparse.ArgumentParser:
    d = {"namespace": "default", "node_selector": None, "problems": 500, "rollouts": 6,
         "concurrency": 500, "claim_concurrency": 500, "cpu": "125m", "memory": "1Gi",
         "runtime_class": "gvisor", "testbed": "/testbed", "probe": "true",
         "strategies": _DEFAULT_STRATEGIES, "recycle": False, "max_reuses": 32,
         "reset_timeout": 30.0, "max_warmpool_size": 1, "warm_per_task": False,
         "colocate": False, "enable_cow": False, "unmanaged_network_policy": False,
         "claim_mode": "per_claim", "overcommit_factor": 3.5,
         "warm_create_budget": 1000, "out": "results/bench.json"}
    d.update(defaults)
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--context", default=None,
                   help="kubeconfig context (out-of-cluster driver); omit with --in-cluster")
    p.add_argument("--in-cluster", action="store_true",
                   help="load in-cluster config from the pod ServiceAccount")
    p.add_argument("--namespace", default=d["namespace"])
    p.add_argument("--node-selector", default=d["node_selector"],
                   help="key=value, e.g. cloud.google.com/gke-nodepool=gvisor-pool")
    if d.get("images_file"):
        p.add_argument("--images-file", default=d["images_file"], help="one image ref per line")
    else:
        p.add_argument("--images-file", required=True, help="one image ref per line")
    p.add_argument("--problems", type=int, default=d["problems"])
    p.add_argument("--rollouts", type=int, default=d["rollouts"], help="tasks per image (RL cycles)")
    p.add_argument("--concurrency", type=int, default=d["concurrency"], help="max concurrent claims held")
    p.add_argument("--claim-concurrency", type=int, default=d["claim_concurrency"],
                   help="max in-flight acquires (recycle variants only)")
    p.add_argument("--strategies", default=d["strategies"],
                   help="comma list of warm-pool strategies to sweep (naive,sliding,pipelined,none)")
    p.add_argument("--recycle", action="store_true", default=d["recycle"],
                   help="also run a recycle (reset-and-reuse, /G claims) variant per strategy")
    p.add_argument("--recycle-only", action="store_true", default=False,
                   help="run ONLY the recycle variant of each strategy")
    p.add_argument("--max-reuses", type=int, default=d["max_reuses"])
    p.add_argument("--max-warmpool-size", type=int, default=d["max_warmpool_size"])
    p.add_argument("--warm-per-task", action="store_true", default=d["warm_per_task"])
    p.add_argument("--colocate", action="store_true", default=d["colocate"])
    p.add_argument("--enable-cow", "--cow", dest="enable_cow", action="store_true",
                   default=d["enable_cow"])
    p.add_argument("--unmanaged-network-policy", action="store_true",
                   default=d["unmanaged_network_policy"],
                   help="set SandboxTemplate spec.networkPolicyManagement=Unmanaged")
    p.add_argument("--reset-timeout", type=float, default=d["reset_timeout"])
    p.add_argument("--cpu", default=d["cpu"])
    p.add_argument("--memory", default=d["memory"])
    p.add_argument("--runtime-class", default=d["runtime_class"])
    p.add_argument("--testbed", default=d["testbed"])
    p.add_argument("--probe", default=d["probe"])
    p.add_argument("--window-size", type=int, default=0)
    p.add_argument("--avg-image-gb", type=float, default=0.0)
    p.add_argument("--usable-disk-gb", type=float, default=0.0)
    p.add_argument("--termination-grace-seconds", type=int, default=-1)
    p.add_argument("--overcommit-factor", type=float, default=d["overcommit_factor"],
                   help="runaway pod breaker multiplier (default 3.5)")
    p.add_argument("--warm-create-budget", type=int, default=d["warm_create_budget"],
                   help="FleetConfig.warm_create_budget (default 1000; 0 = None)")
    # Batch claim mode options
    p.add_argument("--claim-mode", choices=["per_claim", "batch"], default=d["claim_mode"],
                   help="SDK claim mode: per_claim (legacy) or batch (SandboxBatch)")
    p.add_argument("--batch-max-in-flight", type=int, default=0,
                   help="BatchClaimConfig.max_in_flight (0 = SDK default 20)")
    p.add_argument("--batch-create-rps", type=float, default=0.0,
                   help="BatchClaimConfig.create_rps (0 = SDK default 50.0)")
    p.add_argument("--batch-lease-duration", type=int, default=300,
                   help="BatchClaimConfig.lease_duration seconds (default 300)")
    p.add_argument("--batch-work-budget", type=int, default=3600,
                   help="BatchClaimConfig.work_budget seconds (default 3600)")
    p.add_argument("--out", default=d["out"])
    return p


async def _run_variant(strategy, recycle, images, tasks, a, sel, rc):
    cluster = ClusterConfig(name="bench", context=a.context,
                            in_cluster=getattr(a, "in_cluster", False),
                            namespace=a.namespace,
                            node_selector=sel, runtime_class=rc)
    tspec = build_template_spec(a, sel=sel, rc=rc)
    wcb = getattr(a, "warm_create_budget", 1000)

    cfg = FleetConfig(
        clusters=[cluster], max_concurrent=a.concurrency,
        max_warmpool_size=a.max_warmpool_size, warm_per_task=a.warm_per_task,
        placement="image-affinity",
        warm_create_budget=(wcb if wcb > 0 else None),
        overcommit_factor=a.overcommit_factor,
        claim_mode=a.claim_mode,
        batch_claims=build_batch_config(a),
        window_size=(getattr(a, "window_size", 0) or None),
        avg_image_gb=(getattr(a, "avg_image_gb", 0.0) or None),
        node_ephemeral_gb=(getattr(a, "usable_disk_gb", 0.0) or None),
        template=tspec)
    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(tasks))
    label = f"{strategy}{'+recycle' if recycle else ''}"
    win = cfg.window_size or "auto"
    mif = cfg.batch_claims.max_in_flight or "default(20)"
    print(f"[{label}] mode={a.claim_mode}(mif={mif}) {len(images)} images x {a.rollouts} = {len(tasks)} tasks; "
          f"conc={a.concurrency} window={win} wcb={cfg.warm_create_budget} cpu={a.cpu} mem={a.memory}"
          f"{' cow=true' if getattr(a, 'enable_cow', False) else ''}"
          f"{' np=Unmanaged' if tspec.network_policy_management == 'Unmanaged' else ''}", flush=True)

    proc_before = proc_stats()
    t0 = time.monotonic()
    env_delivered: list[float] = []
    verify_out = None
    fatal_error = None
    ok = 0
    err = 0
    errors: list[str] = []
    rep: dict = {}
    wall = 0.0
    proc_after: dict = {}
    try:
        run_kwargs = {"strategy": strategy, "concurrency": a.concurrency}
        if recycle:
            if GitRestoreReset is None:
                raise NotImplementedError("recycle=True requires GitRestoreReset in SDK")
            run_kwargs.update({
                "recycle": recycle, "reset": GitRestoreReset(testbed=a.testbed),
                "max_reuses": a.max_reuses, "reset_timeout": a.reset_timeout,
                "claim_concurrency": a.claim_concurrency,
            })

        def _probe_fn(t, h):
            env_delivered.append(time.monotonic() - t0)
            return h.exec(["bash", "-lc", a.probe])

        try:
            results = await af.run(_probe_fn, **run_kwargs)
            wall = time.monotonic() - t0
            proc_after = proc_stats()
            rep = af.report.to_dict() if af.report else {}
            ok = sum(1 for r in results if not isinstance(r, Exception))
            err = len(results) - ok
            errors = [f"{type(r).__name__}: {r}" for r in results if isinstance(r, Exception)][:8]
        except Exception as run_exc:
            wall = time.monotonic() - t0
            proc_after = proc_stats()
            rep = af.report.to_dict() if af.report else {}
            fatal_error = f"{type(run_exc).__name__}: {run_exc}"
            ok = len(env_delivered)
            err = max(0, len(tasks) - ok)
            errors = [fatal_error]
            print(f"VARIANT FATAL [{label}] after {wall:.1f}s: {fatal_error}", flush=True)
            try:
                await af.release_all()
                await af.teardown()
            except Exception as td_exc:
                print(f"Notice: post-crash teardown warning ({td_exc})", flush=True)
        try:
            verify_out = await af._to_thread(af._fleet.verify_teardown)
        except Exception as ve:
            verify_out = {"error": str(ve)}
    finally:
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(loop.run_in_executor(None, lambda: af.close(wait=False)), timeout=10.0)
        except Exception as e:
            print(f"Notice: af.close() timed out or failed ({e}), continuing", flush=True)

    total = ok + err
    return {
        "strategy": strategy, "recycle": recycle, "label": label,
        "claim_mode": a.claim_mode,
        "batch_max_in_flight": cfg.batch_claims.max_in_flight,
        "wall_s": round(wall, 2),
        "fatal_error": fatal_error,
        "report_total_s": rep.get("total_s"),
        "total_tasks": total, "tasks_ok": ok, "tasks_err": err,
        "claims": rep.get("claims"),
        "s_per_task": round(wall / total, 4) if total else None,
        "wall_per_ok_task_s": round(wall / ok, 4) if ok else None,
        "goodput_tasks_per_s": round(ok / wall, 2) if wall else None,
        "throughput_tasks_per_s": round(total / wall, 2) if wall else None,
        "warm_replicas_peak": rep.get("warm_replicas_peak"),
        "env_ready_ok_s": {
            "n": len(env_delivered),
            "avg": round(sum(env_delivered) / len(env_delivered), 3) if env_delivered else None,
            "p50": _pct(env_delivered, 0.50),
            "p90": _pct(env_delivered, 0.90),
            "p95": _pct(env_delivered, 0.95),
            "p99": _pct(env_delivered, 0.99),
            "max": round(max(env_delivered), 3) if env_delivered else None,
        },
        "phases": _phase_table(rep.get("phases") or {}),
        "phases_err": _phase_table(rep.get("phases_err") or {}),
        "proc_before": proc_before,
        "proc_after": proc_after,
        "verify_teardown": verify_out,
        "errors_sample": errors,
    }


async def run_bench(a) -> dict:
    if not getattr(a, "in_cluster", False) and not a.context:
        raise SystemExit("one of --context (out-of-cluster) or --in-cluster (pod) is required")
    images = [l.strip() for l in open(a.images_file) if l.strip()][:a.problems]
    tasks = [Task(id=f"{_sid(i)}-r{g}", image=i) for i in images for g in range(a.rollouts)]
    sel = _parse_selector(a.node_selector)
    rc = a.runtime_class or None
    variants = _variants(a.strategies, a.recycle, getattr(a, "recycle_only", False))
    labels = [f"{s}+recycle" if r else s for s, r in variants]
    print(f"sweep: {len(variants)} variant(s) = {labels} (claim_mode={a.claim_mode})", flush=True)

    out_file = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    results = []
    for idx, (strategy, recycle) in enumerate(variants):
        if idx > 0:
            await asyncio.sleep(30.0)
        v = await _run_variant(strategy, recycle, images, tasks, a, sel, rc)
        results.append(v)
        print(f"RESULT [{v['label']}] mode={v['claim_mode']} wall_s={v['wall_s']} "
              f"s_per_task={v['s_per_task']} claims={v['claims']} "
              f"ok={v['tasks_ok']} err={v['tasks_err']} "
              f"sockets={v['proc_after'].get('open_sockets')} "
              f"fatal={bool(v.get('fatal_error'))}", flush=True)
        out = {
            "params": {
                "driver": os.path.basename(getattr(a, "_driver", "") or "bench_core"),
                "claim_mode": a.claim_mode,
                "batch_max_in_flight": (getattr(a, "batch_max_in_flight", 0) or None),
                "batch_create_rps": (getattr(a, "batch_create_rps", 0.0) or None),
                "batch_lease_duration": getattr(a, "batch_lease_duration", 300),
                "batch_work_budget": getattr(a, "batch_work_budget", 3600),
                "overcommit_factor": a.overcommit_factor,
                "warm_create_budget": getattr(a, "warm_create_budget", 1000),
                "strategies": a.strategies, "recycle": a.recycle,
                "images_file": a.images_file, "images": len(images), "rollouts": a.rollouts,
                "total_tasks": len(tasks), "concurrency": a.concurrency,
                "claim_concurrency": a.claim_concurrency,
                "max_warmpool_size": a.max_warmpool_size, "warm_per_task": a.warm_per_task,
                "colocate": a.colocate, "enable_cow": getattr(a, "enable_cow", False),
                "unmanaged_network_policy": getattr(a, "unmanaged_network_policy", False),
                "placement": "image-affinity",
                "window_size": (getattr(a, "window_size", 0) or None),
                "avg_image_gb": (getattr(a, "avg_image_gb", 0.0) or None),
                "usable_disk_gb": (getattr(a, "usable_disk_gb", 0.0) or None),
                "termination_grace_seconds": getattr(a, "termination_grace_seconds", -1),
                "max_reuses": a.max_reuses, "reset_timeout": a.reset_timeout,
                "cpu": a.cpu, "memory": a.memory,
                "runtime_class": a.runtime_class, "context": a.context,
                "in_cluster": getattr(a, "in_cluster", False),
                "namespace": a.namespace, "node_selector": sel,
                "testbed": a.testbed, "probe": a.probe, "out": a.out},
            "variants": results,
        }
        with open(out_file, "w") as fh:
            json.dump(out, fh, indent=2)
    print(f"-> {a.out}", flush=True)
    return out


run_recycle = run_bench
