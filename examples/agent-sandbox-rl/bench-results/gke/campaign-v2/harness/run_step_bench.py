"""Step-loop benchmark driver: N synchronous training steps per SDK variant (Campaign 2).

Includes `PARITY_BENCHMARK_SPEC.md` fixes:
  * Uses `bench_core.build_template_spec()` so `--enable-cow`, `--unmanaged-network-policy`,
    and `--termination-grace-seconds` apply uniformly.
  * Emits succeeded-only `phases` + failed-only `phases_err`, `goodput_ok_per_s`,
    `wall_per_ok_task_s`, and `claim_success_rate` per step and overall.
"""
from __future__ import annotations
import asyncio, json, os, sys, time

import bench_core
from agent_sandbox_rl import (AsyncSandboxFleet, ClusterConfig, FleetConfig,
                              GitRestoreReset, Task)
from agent_sandbox_rl.sources import ListSource


def _pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


class _StepProbe:
    """Wraps process_fn: timestamps env delivery (call start = claim complete) and
    task completion, relative to the step start, for the barrier metrics."""

    def __init__(self, probe: str, timeout: float | None = None):
        self.probe = probe
        self.timeout = timeout
        self.step_t0 = 0.0
        self.env_delivered: list[float] = []
        self.task_done: list[float] = []

    def begin_step(self):
        self.step_t0 = time.monotonic()
        self.env_delivered, self.task_done = [], []

    def __call__(self, task: Task, handle):
        self.env_delivered.append(time.monotonic() - self.step_t0)
        out = handle.exec(["bash", "-lc", self.probe], timeout=self.timeout)
        self.task_done.append(time.monotonic() - self.step_t0)
        return out

    def step_metrics(self):
        env, done = self.env_delivered, self.task_done
        return {
            "env_ready_all_s": round(max(env), 2) if env else None,
            "env_ready_avg_s": round(sum(env) / len(env), 2) if env else None,
            "env_ready_p50_s": round(_pct(env, 0.50), 2) if env else None,
            "env_ready_p90_s": round(_pct(env, 0.90), 2) if env else None,
            "env_ready_p95_s": round(_pct(env, 0.95), 2) if env else None,
            "env_ready_p99_s": round(_pct(env, 0.99), 2) if env else None,
            "task_done_all_s": round(max(done), 2) if done else None,
        }


def _phases(rep: dict) -> dict:
    return bench_core._phase_table(rep.get("phases") or {})


def _phases_err(rep: dict) -> dict:
    return bench_core._phase_table(rep.get("phases_err") or {})


def _pool_provisioning(phases: dict) -> dict:
    out = {}
    for k in ("create_warmpool", "wait_pool_ready", "prefetch"):
        if k in phases:
            out[k] = phases[k]
    return out


def _fleet_cfg(a, sel, rc):
    cluster = ClusterConfig(name="bench", context=a.context, namespace=a.namespace,
                            in_cluster=getattr(a, "in_cluster", False),
                            node_selector=sel, runtime_class=rc)
    wcb = getattr(a, "warm_create_budget", 1000)
    return FleetConfig(
        clusters=[cluster], max_concurrent=a.concurrency,
        max_warmpool_size=a.max_warmpool_size, warm_per_task=a.warm_per_task,
        placement="image-affinity",
        warm_create_budget=(wcb if wcb > 0 else None),
        overcommit_factor=a.overcommit_factor,
        claim_mode=a.claim_mode,
        batch_claims=bench_core.build_batch_config(a),
        window_size=(getattr(a, "window_size", 0) or None),
        avg_image_gb=(getattr(a, "avg_image_gb", 0.0) or None),
        node_ephemeral_gb=(getattr(a, "usable_disk_gb", 0.0) or None),
        template=bench_core.build_template_spec(a, sel=sel, rc=rc))


async def _bounded_gather(n, coros):
    sem = asyncio.Semaphore(max(1, n))

    async def _guard(c):
        async with sem:
            return await c

    return await asyncio.gather(*(_guard(c) for c in coros), return_exceptions=True)


class _CleanOutcome:
    clean, seconds, reason = True, 0.0, "no_reset_needed"


async def _noop_reset():
    return _CleanOutcome()


async def _run_hold_steps(images, tasks, a, sel, rc):
    af = AsyncSandboxFleet(_fleet_cfg(a, sel, rc))
    G = max(1, a.rollouts)
    img_tasks = [Task(id=bench_core._sid(img), image=img) for img in images]
    af.load_tasks(ListSource(img_tasks))
    reset = GitRestoreReset(testbed=a.testbed)
    probe = _StepProbe(a.probe, timeout=a.exec_timeout)
    exec_conc = a.exec_concurrency or a.concurrency
    label = "hold"
    print(f"[{label}] mode={a.claim_mode} {len(images)} images (held) x {G} rollouts x {a.steps} steps; "
          f"conc={a.concurrency} exec_conc={exec_conc} dwell={a.step_dwell}s", flush=True)

    def _reset_one(handle, base):
        return reset.reset(handle, base, timeout=a.reset_timeout)

    async def _reset_and_replace(step):
        outcomes = await _bounded_gather(
            a.concurrency,
            [af._to_thread(_reset_one, h, baselines[id(h)])
             if baselines[id(h)] is not None else _noop_reset()
             for h in handles])
        secs, repl = [], 0
        for i, (h, oc) in enumerate(zip(list(handles), outcomes)):
            clean = (not isinstance(oc, Exception)) and getattr(oc, "clean", False)
            if not isinstance(oc, Exception) and oc is not None:
                secs.append(oc.seconds)
            if not clean:
                try:
                    await af.release(h)
                except Exception:
                    pass
                try:
                    nh = await af.acquire(img_tasks[i])
                    baselines[id(nh)] = await af._to_thread(reset.prime, nh)
                    handles[i] = nh
                    repl += 1
                except Exception as e:
                    errors.append(f"s{step} replace failed: {e}")
        return secs, repl

    steps_out, errors = [], []
    proc_before = bench_core.proc_stats()
    variant_t0 = time.monotonic()
    handles, baselines = [], {}
    verify_out = None
    try:
        await af.setup()
        t_setup = time.monotonic()
        handles = await af.acquire_batch(img_tasks)
        acquire_all_s = time.monotonic() - t_setup
        prime_res = await _bounded_gather(
            a.claim_concurrency,
            [af._to_thread(reset.prime, h) for h in handles])
        for h, b in zip(handles, prime_res):
            baselines[id(h)] = b if not isinstance(b, Exception) else None
        drained = await _bounded_gather(
            a.claim_concurrency,
            [af._to_thread(af._fleet.set_pool_replicas, img, 0) for img in images])
        drain_err = sum(1 for r in drained if isinstance(r, Exception))
        setup_all_s = time.monotonic() - t_setup
        non_recyclable = sum(1 for b in baselines.values()
                             if b is None or not GitRestoreReset.recyclable(b))
        print(f"  SETUP {len(handles)} sandboxes in {setup_all_s:.1f}s "
              f"(acquire {acquire_all_s:.1f}s + prime/drain {setup_all_s - acquire_all_s:.1f}s); "
              f"{non_recyclable} non-recyclable /testbed; "
              f"{len(images)} pools drained ({drain_err} err)", flush=True)

        for step in range(a.steps):
            last_step = step == a.steps - 1
            t0 = time.monotonic()
            ok = err = 0
            exec_all_s = reset_all_s = 0.0
            reset_secs, replacements, step_env = [], 0, {}
            for g in range(G):
                probe.begin_step()
                te = time.monotonic()
                results = await _bounded_gather(
                    exec_conc,
                    [af._to_thread(probe, t, h) for t, h in zip(img_tasks, handles)])
                exec_all_s += time.monotonic() - te
                ok += sum(1 for r in results if not isinstance(r, Exception))
                err += sum(1 for r in results if isinstance(r, Exception))
                errors += [f"s{step}r{g} exec {type(r).__name__}: {r}"
                           for r in results if isinstance(r, Exception)][:2]
                if g == 0:
                    step_env = probe.step_metrics()
                if not (last_step and g == G - 1):
                    tr = time.monotonic()
                    secs, repl = await _reset_and_replace(step)
                    reset_all_s += time.monotonic() - tr
                    reset_secs += secs
                    replacements += repl
            if a.step_dwell > 0 and not last_step:
                await asyncio.sleep(max(0.0, a.step_dwell - reset_all_s))
            wall = time.monotonic() - t0
            ps = bench_core.proc_stats()
            s = {"step": step, "step_wall_s": round(wall, 2), **step_env,
                 "tasks": len(images) * G, "ok": ok, "err": err,
                 "goodput_ok_per_s": round(ok / wall, 3) if wall > 0 else None,
                 "wall_per_ok_task_s": round(wall / ok, 4) if ok > 0 else None,
                 "claims": (len(images) if step == 0 else replacements),
                 "exec_all_s": round(exec_all_s, 2),
                 "reset_all_s": round(reset_all_s, 2),
                 "reset": ({"n": len(reset_secs),
                            "avg": round(sum(reset_secs) / len(reset_secs), 3),
                            "max": round(max(reset_secs), 3)} if reset_secs else None),
                 "replacements": replacements,
                 "acquire_all_s": round(acquire_all_s, 2) if step == 0 else 0.0,
                 "setup_all_s": round(setup_all_s, 2) if step == 0 else 0.0,
                 "step_dwell_s": a.step_dwell,
                 "proc": ps}
            steps_out.append(s)
            print(f"  STEP {step}: wall={s['step_wall_s']}s exec={s['exec_all_s']}s "
                  f"reset={s['reset_all_s']}s env_all={s.get('env_ready_all_s')}s "
                  f"claims={s['claims']} ok={ok}/{len(images) * G} "
                  f"sockets={ps['open_sockets']}", flush=True)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception as td_err:
            print(f"[{label}] cleanup after failure ALSO failed: {td_err}", flush=True)
        raise
    else:
        await af.release_all()
        await af.teardown()
        try:
            verify_out = await af._to_thread(af._fleet.verify_teardown)
        except Exception as ve:
            verify_out = {"error": str(ve)}
    finally:
        af.close()

    return {"strategy": "hold", "recycle": True, "label": label,
            "claim_mode": a.claim_mode,
            "variant_wall_s": round(time.monotonic() - variant_t0, 2),
            "proc_before": proc_before, "proc_after": bench_core.proc_stats(),
            "verify_teardown": verify_out,
            "steps": steps_out, "errors_sample": errors[:8]}


async def _run_variant_steps(strategy, recycle, images, tasks, a, sel, rc):
    cfg = _fleet_cfg(a, sel, rc)
    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(tasks))
    label = f"{strategy}{'+recycle' if recycle else ''}"
    probe = _StepProbe(a.probe, timeout=a.exec_timeout)
    mif = cfg.batch_claims.max_in_flight or "default(20)"
    print(f"[{label}] mode={a.claim_mode}(mif={mif}) {len(images)} images x {a.rollouts} x {a.steps} steps; "
          f"conc={a.concurrency} warm_per_task={a.warm_per_task} max_wp={a.max_warmpool_size}"
          f"{' cow=true' if getattr(a, 'enable_cow', False) else ''}"
          f"{' np=Unmanaged' if cfg.template.network_policy_management == 'Unmanaged' else ''}",
          flush=True)

    steps_out, errors = [], []
    proc_before = bench_core.proc_stats()
    variant_t0 = time.monotonic()
    verify_out = None
    try:
        for step in range(a.steps):
            last = step == a.steps - 1
            probe.begin_step()
            t0 = time.monotonic()
            results = await af.run(
                probe, strategy=strategy, concurrency=a.concurrency,
                keep_warm=not last,
                recycle=recycle, reset=GitRestoreReset(testbed=a.testbed),
                max_reuses=a.max_reuses, reset_timeout=a.reset_timeout,
                shards_per_image=getattr(a, "shards_per_image", 1),
                claim_concurrency=a.claim_concurrency)
            wall = time.monotonic() - t0
            ps = bench_core.proc_stats()
            rep = af.report.to_dict() if af.report else {}
            ph = _phases(rep)
            ph_err = _phases_err(rep)
            ok = sum(1 for r in results if not isinstance(r, Exception))
            err = len(results) - ok
            tot = len(results)
            errors += [f"s{step} {type(r).__name__}: {r}"
                       for r in results if isinstance(r, Exception)][:4]
            s = {"step": step, "step_wall_s": round(wall, 2),
                 **probe.step_metrics(),
                 "tasks": tot, "ok": ok, "err": err,
                 "claim_success_rate": round(ok / tot, 4) if tot > 0 else None,
                 "goodput_ok_per_s": round(ok / wall, 3) if wall > 0 else None,
                 "wall_per_ok_task_s": round(wall / ok, 4) if ok > 0 else None,
                 "claims": rep.get("claims"),
                 "pool_provisioning": _pool_provisioning(ph),
                 "claim": ph.get("claim"),
                 "claim_err": ph_err.get("claim"),
                 "process": ph.get("process"),
                 "release": ph.get("release"), "reset": ph.get("reset"),
                 "teardown": ph.get("teardown"),
                 "phases_err": ph_err,
                 "proc": ps}
            steps_out.append(s)
            print(f"  STEP {step}: wall={s['step_wall_s']}s "
                  f"env_ready_all={s['env_ready_all_s']}s p50={s['env_ready_p50_s']}s "
                  f"p99={s['env_ready_p99_s']}s claims={s['claims']} "
                  f"ok={ok}/{tot} goodput={s['goodput_ok_per_s']}/s "
                  f"sockets={ps['open_sockets']} rss={ps['vm_rss_mb']}MB",
                  flush=True)
        try:
            verify_out = await af._to_thread(af._fleet.verify_teardown)
        except Exception as ve:
            verify_out = {"error": str(ve)}
    except BaseException:
        try:
            await af.teardown()
        except Exception as td_err:
            print(f"[{label}] teardown after failure ALSO failed: {td_err}", flush=True)
        raise
    finally:
        af.close()

    variant_wall = round(time.monotonic() - variant_t0, 2)
    tot_ok = sum(s["ok"] for s in steps_out)
    tot_tasks = sum(s["tasks"] for s in steps_out)
    return {"strategy": strategy, "recycle": recycle, "label": label,
            "claim_mode": a.claim_mode,
            "batch_max_in_flight": cfg.batch_claims.max_in_flight,
            "variant_wall_s": variant_wall,
            "total_ok": tot_ok,
            "total_tasks": tot_tasks,
            "claim_success_rate": round(tot_ok / tot_tasks, 4) if tot_tasks > 0 else None,
            "goodput_ok_per_s": round(tot_ok / variant_wall, 3) if variant_wall > 0 else None,
            "wall_per_ok_task_s": round(variant_wall / tot_ok, 4) if tot_ok > 0 else None,
            "proc_before": proc_before,
            "proc_after": bench_core.proc_stats(),
            "verify_teardown": verify_out,
            "steps": steps_out, "errors_sample": errors[:8]}


async def _main(a):
    if not getattr(a, "in_cluster", False) and not a.context:
        raise SystemExit("one of --context (out-of-cluster) or --in-cluster (pod) is required")
    images = [l.strip() for l in open(a.images_file) if l.strip()][:a.problems]
    tasks = [Task(id=f"{bench_core._sid(i)}-r{g}", image=i)
             for i in images for g in range(a.rollouts)]
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None
    if a.hold:
        print(f"step sweep: hold variant, {a.steps} steps (claim_mode={a.claim_mode})", flush=True)
        out_variants = [await _run_hold_steps(images, tasks, a, sel, rc)]
    else:
        variants = bench_core._variants(a.strategies, a.recycle)
        if getattr(a, "recycle_only", False):
            variants = [(s, True) for s, _r in bench_core._variants(a.strategies, False)]
        print(f"step sweep: {len(variants)} variant(s), {a.steps} steps each (claim_mode={a.claim_mode})",
              flush=True)
        out_variants = []
        for strategy, recycle in variants:
            out_variants.append(
                await _run_variant_steps(strategy, recycle, images, tasks, a, sel, rc))

    out = {"params": {**{k: v for k, v in vars(a).items() if k != "_driver"},
                      "driver": "run_step_bench", "images": len(images),
                      "tasks_per_step": len(tasks),
                      "node_selector_parsed": sel},
           "variants": out_variants}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(out, fh, indent=2)
    for v in out_variants:
        walls = [s["step_wall_s"] for s in v["steps"]]
        envs = [s["env_ready_all_s"] for s in v["steps"]]
        socks = [s["proc"]["open_sockets"] for s in v["steps"]]
        print(f"RESULT [{v['label']}] mode={v['claim_mode']} step_walls={walls} "
              f"env_ready_all={envs} sockets={socks}", flush=True)
    print(f"-> {a.out}", flush=True)
    if any(sum(s["ok"] for s in v["steps"]) == 0 for v in out_variants):
        sys.exit(3)


def main():
    p = bench_core.build_parser(__doc__.splitlines()[0], rollouts=1,
                                namespace="agent-sandbox-rl")
    p.add_argument("--steps", type=int, default=8,
                   help="synchronous steps per variant (barrier between steps)")
    p.add_argument("--shards-per-image", type=int, default=1)
    p.add_argument("--hold", action="store_true")
    p.add_argument("--step-dwell", type=float, default=0.0)
    p.add_argument("--exec-concurrency", type=int, default=0)
    p.add_argument("--exec-timeout", type=float, default=120.0)
    a = p.parse_args()
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
