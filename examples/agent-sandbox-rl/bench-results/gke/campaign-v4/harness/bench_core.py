"""Shared benchmark engine for the sandbox-rl-performance drivers.

Drives `AsyncSandboxFleet.run(strategy=..., recycle=...)` with non-invasive, single-clock
paired per-sandbox TTFI (`wire_claim_s`, `first_cmd_exec_s`, `ttfi_s = t_cmd_done - t_claim_start`),
`straggler_skew` (`max / p50`), multi-cluster `--contexts` support, process resource snapshots
(open sockets, RSS), and configurable `--tmp-disk-gi` ephemeral `/tmp` volume sizing.

Backwards-compatible: runs smoothly on both upstream merged SDKs and batch-enabled SDKs.
"""
from __future__ import annotations
import argparse, asyncio, dataclasses, hashlib, json, os, threading, time

from kubernetes import client as k8s_client
from agent_sandbox_rl import (AsyncSandboxFleet, ClusterConfig, FleetConfig,
                              ResourceSpec, TemplateSpec, Task)
try:
    from agent_sandbox_rl import BatchClaimConfig
except ImportError:
    BatchClaimConfig = None

try:
    from agent_sandbox_rl import GitRestoreReset
except ImportError:
    GitRestoreReset = None
from agent_sandbox_rl.sources import ListSource

_DEFAULT_STRATEGIES = "naive,sliding,pipelined"


def _has_field(cls, name: str) -> bool:
    if dataclasses.is_dataclass(cls):
        return any(f.name == name for f in dataclasses.fields(cls))
    fields = getattr(cls, "model_fields", getattr(cls, "__fields__", {}))
    return name in fields or hasattr(cls, name)


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


BATCH_ID_LABEL = "agents.x-k8s.io/batch-id"


def _sid(image: str) -> str:
    h = hashlib.md5(image.encode(), usedforsecurity=False).hexdigest()[:8]
    slug = image.rsplit("/", 1)[-1].split("@", 1)[0].replace(":", "-")[:51].rstrip("-")
    return f"{slug}-{h}"


def exec_checked(handle, probe: str, timeout: float | None = None) -> str:
    """Execute `probe` in a bash subshell and raise RuntimeError on non-zero exit code."""
    wrapped = f"(\n{probe}\n) && printf '\\n__ASRL_OK__\\n'"
    out = handle.exec(["bash", "-lc", wrapped], timeout=timeout)
    raw_out = out if isinstance(out, str) else (getattr(out, "stdout", "") or "")
    if "__ASRL_OK__" not in raw_out:
        raise RuntimeError(f"probe non-zero exit: {raw_out.strip()[:200]!r}")
    return raw_out


def _close_api_client(ac) -> None:
    if ac is None:
        return
    rest = getattr(ac, "rest_client", None)
    pm = getattr(rest, "pool_manager", None)
    clear_fn = getattr(pm, "clear", None)
    if callable(clear_fn):
        try:
            clear_fn()
        except Exception:
            pass
    close_fn = getattr(ac, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


def close_fleet(af: AsyncSandboxFleet, *, wait: bool = False) -> None:
    """Shut down `AsyncSandboxFleet` executor and close underlying urllib3 `ApiClient` pools."""
    try:
        af.close(wait=wait)
    except Exception as exc:
        print(f"Notice: af.close() warning ({exc})", flush=True)
    for c in getattr(af, "registry", ()) or ():
        _close_api_client(getattr(c, "api_client", None))
        helper = getattr(c, "_k8s_helper", None)
        if helper is not None:
            for attr in ("coordination_v1_api", "_batch_custom_objects_api"):
                sub = getattr(helper, attr, None)
                _close_api_client(getattr(sub, "api_client", None))


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
    return round(s[idx], 4)


def quantiles(vals: list[float]) -> dict:
    """Standard product-metric distribution summary (First/min, p50, avg, p90, p95, p99, max/P100, skew)."""
    if not vals:
        return {"n": 0, "first": None, "min": None, "p50": None, "avg": None,
                "p90": None, "p95": None, "p99": None, "max": None, "straggler_skew": None}
    p50 = _pct(vals, 0.50)
    mx = round(max(vals), 4)
    mn = round(min(vals), 4)
    return {
        "n": len(vals),
        "first": mn,
        "min": mn,
        "p50": p50,
        "avg": round(sum(vals) / len(vals), 4),
        "p90": _pct(vals, 0.90),
        "p95": _pct(vals, 0.95),
        "p99": _pct(vals, 0.99),
        "max": mx,
        "straggler_skew": round(mx / p50, 3) if p50 and p50 > 0 else None,
    }


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


def build_batch_config(a):
    if getattr(a, "claim_mode", "per_claim") != "batch":
        return None
    if BatchClaimConfig is None or not _has_field(FleetConfig, "claim_mode"):
        raise RuntimeError(
            "claim_mode='batch' requires an agent_sandbox_rl build with "
            "FleetConfig.claim_mode and BatchClaimConfig"
        )
    mif = getattr(a, "batch_max_in_flight", 0) or None
    crps = getattr(a, "batch_create_rps", 0.0) or None
    return BatchClaimConfig(
        create_rps=crps,
        max_in_flight=mif,
        lease_duration=getattr(a, "batch_lease_duration", 300),
        work_budget=getattr(a, "batch_work_budget", 3600),
    )


def apply_claim_mode_kwargs(cfg_kwargs: dict, a) -> dict:
    """Populate `claim_mode` / `batch_claims` on `FleetConfig` kwargs, failing fast if `--claim-mode batch` is unsupported."""
    mode = getattr(a, "claim_mode", None) or "per_claim"
    if mode == "batch":
        bc = build_batch_config(a)
        cfg_kwargs["claim_mode"] = "batch"
        if bc is not None and _has_field(FleetConfig, "batch_claims"):
            cfg_kwargs["batch_claims"] = bc
    elif _has_field(FleetConfig, "claim_mode") and mode:
        cfg_kwargs["claim_mode"] = mode
    return cfg_kwargs


def build_clusters(a, *, sel, rc) -> list[ClusterConfig]:
    ctxs_raw = getattr(a, "contexts", None)
    if ctxs_raw:
        ctxs = [c.strip() for c in ctxs_raw.split(",") if c.strip()]
        return [
            ClusterConfig(
                name=f"c{i+1}",
                context=ctx,
                in_cluster=False,
                namespace=a.namespace,
                node_selector=sel,
                runtime_class=rc,
            )
            for i, ctx in enumerate(ctxs)
        ]
    return [
        ClusterConfig(
            name="bench",
            context=a.context,
            in_cluster=getattr(a, "in_cluster", False),
            namespace=a.namespace,
            node_selector=sel,
            runtime_class=rc,
        )
    ]


def build_template_spec(a, *, sel, rc) -> TemplateSpec:
    """Shared TemplateSpec builder honoring --enable-cow, --tmp-disk-gi, --unmanaged-network-policy, and --termination-grace-seconds."""
    cow_flag = getattr(a, "enable_cow", False)
    if isinstance(cow_flag, str):
        cow_flag = cow_flag.lower() in ("true", "1", "yes")
    tmp_disk_gi = getattr(a, "tmp_disk_gi", 0) or 0

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
    elif tmp_disk_gi > 0:
        extra_pod_spec = {
            "containers": [{
                "name": "agent-runtime",
                "env": [
                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                    {"name": "TMPDIR", "value": "/tmp"},
                ],
                "volumeMounts": [
                    {"name": "disk-scratch", "mountPath": "/tmp"},
                ],
            }],
            "volumes": [
                {"name": "disk-scratch", "emptyDir": {"sizeLimit": f"{int(tmp_disk_gi)}Gi"}}
            ],
        }

    tgs = getattr(a, "termination_grace_seconds", -1)
    if tgs is not None and tgs >= 0:
        extra_pod_spec["terminationGracePeriodSeconds"] = tgs

    tspec_kwargs = {
        "runtime_class": rc,
        "node_selector": sel,
        "resources": ResourceSpec(cpu=a.cpu, memory=a.memory),
        "extra_pod_spec": extra_pod_spec,
    }
    if _has_field(TemplateSpec, "colocate_replicas"):
        tspec_kwargs["colocate_replicas"] = getattr(a, "colocate", False)
    if _has_field(TemplateSpec, "network_policy_management"):
        np_mode = "Unmanaged" if getattr(a, "unmanaged_network_policy", False) else None
        tspec_kwargs["network_policy_management"] = np_mode
    if _has_field(TemplateSpec, "automount_service_account_token"):
        tspec_kwargs["automount_service_account_token"] = False

    return TemplateSpec(**tspec_kwargs)


def build_parser(description: str, **defaults) -> argparse.ArgumentParser:
    d = {"namespace": "default", "node_selector": None, "problems": 500, "rollouts": 6,
         "concurrency": 500, "claim_concurrency": 500, "cpu": "125m", "memory": "1Gi",
         "runtime_class": "gvisor", "testbed": "/testbed", "probe": "true",
         "strategies": _DEFAULT_STRATEGIES, "recycle": False, "max_reuses": 32,
         "reset_timeout": 30.0, "max_warmpool_size": 1, "warm_per_task": False,
         "colocate": False, "enable_cow": False, "tmp_disk_gi": 0,
         "unmanaged_network_policy": False, "placement": "image-affinity",
         "claim_mode": "per_claim", "overcommit_factor": 3.5,
         "warm_create_budget": 1000, "out": "results/bench.json"}
    d.update(defaults)
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--context", default=None,
                   help="kubeconfig context (single out-of-cluster driver)")
    p.add_argument("--contexts", default=None,
                   help="comma-separated kubeconfig contexts for multi-cluster fleet")
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
    p.add_argument("--tmp-disk-gi", type=int, default=d["tmp_disk_gi"],
                   help="mount a disk-backed emptyDir of N GiB at /tmp when >0 (Eval Doc §3 profile)")
    p.add_argument("--unmanaged-network-policy", action="store_true",
                   default=d["unmanaged_network_policy"],
                   help="set SandboxTemplate spec.networkPolicyManagement=Unmanaged")
    p.add_argument("--placement", default=d["placement"],
                   choices=["image-affinity", "round-robin", "least-loaded", "capacity-weighted"])
    p.add_argument("--reset-timeout", type=float, default=d["reset_timeout"])
    p.add_argument("--cpu", default=d["cpu"])
    p.add_argument("--memory", default=d["memory"])
    p.add_argument("--runtime-class", default=d["runtime_class"])
    p.add_argument("--testbed", default=d["testbed"])
    p.add_argument("--probe", default=d["probe"])
    p.add_argument("--window-size", type=int, default=0)
    p.add_argument("--avg-image-gb", type=float, default=0.0)
    p.add_argument("--usable-disk-gb", type=float, default=0.0)
    p.add_argument("--termination-grace-seconds", type=int,
                   default=d.get("termination_grace_seconds", -1))
    p.add_argument("--overcommit-factor", type=float, default=d["overcommit_factor"],
                   help="runaway pod breaker multiplier (default 3.5)")
    p.add_argument("--warm-create-budget", type=int, default=d["warm_create_budget"],
                   help="FleetConfig.warm_create_budget (default 1000; 0 = warm all at once)")
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
    clusters = build_clusters(a, sel=sel, rc=rc)
    tspec = build_template_spec(a, sel=sel, rc=rc)
    wcb = getattr(a, "warm_create_budget", 1000)

    cfg_kwargs = {
        "clusters": clusters,
        "max_concurrent": a.concurrency,
        "max_warmpool_size": a.max_warmpool_size,
        "warm_per_task": a.warm_per_task,
        "placement": getattr(a, "placement", "image-affinity"),
        "warm_create_budget": max(0, int(wcb)),
        "overcommit_factor": a.overcommit_factor,
        "window_size": (getattr(a, "window_size", 0) or None),
        "avg_image_gb": (getattr(a, "avg_image_gb", 0.0) or None),
        "node_ephemeral_gb": (getattr(a, "usable_disk_gb", 0.0) or None),
        "template": tspec,
    }
    apply_claim_mode_kwargs(cfg_kwargs, a)
    cfg = FleetConfig(**cfg_kwargs)
    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(tasks))

    # Non-invasive paired per-sandbox TTFI shim on af._fleet.acquire
    t_lock = threading.Lock()
    handle_acq_times: dict[int, tuple[float, float]] = {}
    paired_wire_claim_s: list[float] = []
    paired_first_cmd_s: list[float] = []
    paired_ttfi_s: list[float] = []

    orig_acquire = af._fleet.acquire

    def _timed_acquire(task, *args, **kwargs):
        ts = time.monotonic()
        h = orig_acquire(task, *args, **kwargs)
        te = time.monotonic()
        with t_lock:
            handle_acq_times[id(h)] = (ts, te)
        return h

    af._fleet.acquire = _timed_acquire  # type: ignore[method-assign]

    label = f"{strategy}{'+recycle' if recycle else ''}"
    win = cfg.window_size or "auto"
    mif = "n/a"
    if getattr(cfg, "batch_claims", None):
        mif = getattr(cfg.batch_claims, "max_in_flight", "default(20)")

    print(f"[{label}] mode={getattr(a, 'claim_mode', 'per_claim')}(mif={mif}) clusters={len(clusters)} "
          f"{len(images)} images x {a.rollouts} = {len(tasks)} tasks; "
          f"conc={a.concurrency} window={win} wcb={cfg.warm_create_budget} cpu={a.cpu} mem={a.memory}",
          flush=True)

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
            now_deliv = time.monotonic()
            with t_lock:
                env_delivered.append(now_deliv - t0)
                acq_pair = handle_acq_times.pop(id(h), None)
            tc0 = time.monotonic()
            out = exec_checked(h, a.probe)
            tc1 = time.monotonic()
            if acq_pair is not None:
                ts, te = acq_pair
                with t_lock:
                    paired_wire_claim_s.append(te - ts)
                    paired_first_cmd_s.append(tc1 - tc0)
                    paired_ttfi_s.append(tc1 - ts)
            return out

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
        verify_out = await safe_verify_teardown(af)
    finally:
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(loop.run_in_executor(None, lambda: close_fleet(af, wait=False)), timeout=10.0)
        except Exception as e:
            print(f"Notice: close_fleet() timed out or failed ({e}), continuing", flush=True)

    total = ok + err
    ttfi_q = quantiles(paired_ttfi_s)
    batch_mif = None
    if getattr(cfg, "batch_claims", None):
        batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)
    n_clusters = max(1, len(clusters))
    goodput = round(ok / wall, 2) if wall else None
    tput = round(total / wall, 2) if wall else None

    return {
        "strategy": strategy, "recycle": recycle, "label": label,
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "batch_max_in_flight": batch_mif,
        "clusters": len(clusters),
        "wall_s": round(wall, 2),
        "fatal_error": fatal_error,
        "report_total_s": rep.get("total_s"),
        "total_tasks": total, "tasks_ok": ok, "tasks_err": err,
        "failure_rate_pct": round(100.0 * err / total, 5) if total else None,
        "claims": rep.get("claims"),
        "s_per_task": round(wall / total, 4) if total else None,
        "wall_per_ok_task_s": round(wall / ok, 4) if ok else None,
        "goodput_tasks_per_s": goodput,
        "per_cluster_goodput_tasks_per_s": round(goodput / n_clusters, 2) if goodput is not None else None,
        "throughput_tasks_per_s": tput,
        "per_cluster_throughput_tasks_per_s": round(tput / n_clusters, 2) if tput is not None else None,
        "warm_replicas_peak": rep.get("warm_replicas_peak"),
        "paired_ttfi_s": ttfi_q,
        "paired_wire_claim_s": quantiles(paired_wire_claim_s),
        "paired_first_cmd_s": quantiles(paired_first_cmd_s),
        "env_ready_ok_s": quantiles(env_delivered),
        "phases": _phase_table(rep.get("phases") or {}),
        "phases_err": _phase_table(rep.get("phases_err") or {}),
        "proc_before": proc_before,
        "proc_after": proc_after,
        "verify_teardown": verify_out,
        "errors_sample": errors,
    }


def _count_cluster_objects_sync(c) -> dict[str, int]:
    """Count all workload resources in `c.namespace` across both `main` and batch-hardened SDKs."""
    pods = c.core_api.list_namespaced_pod(c.namespace)
    rem_pods = sum(1 for p in pods.items if not p.metadata.name.startswith("asrl-"))
    rem_claims = len(c.resources._list_objects("sandboxclaims"))
    rem_wps = len(c.resources._list_objects("sandboxwarmpools"))
    rem_tmpls = len(c.resources._list_objects("sandboxtemplates"))

    api_client = getattr(c, "api_client", None) or c.core_api.api_client
    custom_api = (
        getattr(c, "custom_api", None)
        or getattr(c.resources, "custom_api", None)
        or k8s_client.CustomObjectsApi(api_client)
    )
    sbx_resp = custom_api.list_namespaced_custom_object(
        "agents.x-k8s.io", "v1beta1", c.namespace, "sandboxes"
    )
    rem_sbxs = len(sbx_resp.get("items", []))

    coord_api = (
        getattr(c, "coordination_api", None)
        or getattr(c, "coordination_v1_api", None)
        or k8s_client.CoordinationV1Api(api_client)
    )
    rem_leases = 0
    try:
        lease_resp = coord_api.list_namespaced_lease(
            c.namespace,
            label_selector=BATCH_ID_LABEL,
        )
        rem_leases = len(lease_resp.items)
    except k8s_client.ApiException as exc:
        if exc.status not in (403, 404):
            raise

    return {
        "pods": rem_pods,
        "sandboxes": rem_sbxs,
        "claims": rem_claims,
        "warmpools": rem_wps,
        "templates": rem_tmpls,
        "batch_leases": rem_leases,
    }


async def safe_verify_teardown(af: AsyncSandboxFleet) -> dict:
    """Backwards-compatible `verify_teardown` supporting both `main` and batch-hardened `SandboxFleet`."""
    try:
        vt_fn = getattr(af._fleet, "verify_teardown", None)
        if callable(vt_fn):
            return await af._to_thread(vt_fn)
        out: dict = {}
        for c in af.registry:
            counts = await af._to_thread(_count_cluster_objects_sync, c)
            out[c.name] = {"remaining": counts}
        return out
    except Exception as ve:
        return {"error": f"{type(ve).__name__}: {ve}"}


async def wait_clusters_drained(
    clusters: list[ClusterConfig],
    timeout: float = 240.0,
    settle_s: float = 30.0,
    require_converged: bool = True,
) -> dict:
    """Wait until all non-driver Pods, Sandboxes, SandboxClaims, SandboxWarmPools, SandboxTemplates, and batch Leases reach 0 across clusters, then settle."""
    probe_cfg = FleetConfig(clusters=clusters, max_concurrent=1)
    probe_af = AsyncSandboxFleet(probe_cfg)
    t0 = time.monotonic()
    converged = False
    last_counts: dict[str, int] = {}
    try:
        while time.monotonic() - t0 < timeout:
            agg = {
                "pods": 0,
                "sandboxes": 0,
                "claims": 0,
                "warmpools": 0,
                "templates": 0,
                "batch_leases": 0,
            }
            for c in probe_af.registry:
                c_counts = await probe_af._to_thread(_count_cluster_objects_sync, c)
                for k, v in c_counts.items():
                    agg[k] += v
            last_counts = agg
            if all(v == 0 for v in last_counts.values()):
                converged = True
                break
            await asyncio.sleep(1.5)
    finally:
        close_fleet(probe_af, wait=False)
    drain_s = round(time.monotonic() - t0, 3)
    if not converged and require_converged:
        raise TimeoutError(
            f"clusters failed to drain within {timeout}s (elapsed={drain_s}s): remaining={last_counts}"
        )
    if settle_s > 0:
        await asyncio.sleep(settle_s)
    return {"converged": converged, "drain_s": drain_s, "settle_s": settle_s, "remaining": last_counts}


async def run_bench(a) -> dict:
    if not getattr(a, "in_cluster", False) and not a.context and not getattr(a, "contexts", None):
        raise SystemExit("one of --context, --contexts, or --in-cluster is required")
    images = [l.strip() for l in open(a.images_file) if l.strip()][:a.problems]
    tasks = [Task(id=f"{_sid(i)}-r{g}", image=i) for i in images for g in range(a.rollouts)]
    sel = _parse_selector(a.node_selector)
    rc = a.runtime_class or None
    clusters = build_clusters(a, sel=sel, rc=rc)
    variants = _variants(a.strategies, a.recycle, getattr(a, "recycle_only", False))
    labels = [f"{s}+recycle" if r else s for s, r in variants]
    print(f"sweep: {len(variants)} variant(s) = {labels} (claim_mode={getattr(a, 'claim_mode', 'per_claim')})", flush=True)

    out_file = os.path.abspath(a.out)
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    results = []
    for idx, (strategy, recycle) in enumerate(variants):
        if idx > 0:
            drain_info = await wait_clusters_drained(clusters, timeout=240.0, settle_s=30.0)
            print(f"  [inter-variant drain] {drain_info}", flush=True)
        v = await _run_variant(strategy, recycle, images, tasks, a, sel, rc)
        results.append(v)
        ttfi = v["paired_ttfi_s"]
        print(f"RESULT [{v['label']}] mode={v['claim_mode']} wall_s={v['wall_s']} "
              f"rate={v['goodput_tasks_per_s']}/s ok={v['tasks_ok']} err={v['tasks_err']} "
              f"ttfi(first={ttfi['first']} p50={ttfi['p50']} avg={ttfi['avg']} "
              f"p99={ttfi['p99']} max={ttfi['max']}) "
              f"sockets={v['proc_after'].get('open_sockets')}", flush=True)
        out = {
            "params": {
                "driver": os.path.basename(getattr(a, "_driver", "") or "bench_core"),
                "claim_mode": getattr(a, "claim_mode", "per_claim"),
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
                "tmp_disk_gi": getattr(a, "tmp_disk_gi", 0),
                "unmanaged_network_policy": getattr(a, "unmanaged_network_policy", False),
                "placement": getattr(a, "placement", "image-affinity"),
                "window_size": (getattr(a, "window_size", 0) or None),
                "termination_grace_seconds": getattr(a, "termination_grace_seconds", -1),
                "cpu": a.cpu, "memory": a.memory,
                "runtime_class": a.runtime_class, "context": a.context,
                "contexts": getattr(a, "contexts", None),
                "in_cluster": getattr(a, "in_cluster", False),
                "namespace": a.namespace, "node_selector": sel,
                "testbed": a.testbed, "probe": a.probe, "out": a.out},
            "variants": results,
        }
        with open(out_file, "w") as fh:
            json.dump(out, fh, indent=2)
    print(f"-> {a.out}", flush=True)
    return out


# Back-compat alias: drivers historically imported run_recycle.
run_recycle = run_bench
