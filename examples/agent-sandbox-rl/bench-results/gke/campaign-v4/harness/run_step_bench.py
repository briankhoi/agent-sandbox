"""Step-loop benchmark driver: N synchronous training steps per SDK variant.

Models what a synchronous RL training loop experiences: each STEP runs every task
(images x rollouts) and BARRIERS before the next step. One fleet session per
variant — ``AsyncSandboxFleet.run(..., keep_warm=True)`` per step defers teardown
to the last step. Pool persistence across steps is strategy-authentic:
``naive`` keeps its pools resident (steps 2+ re-verify readiness only);
``sliding``/``pipelined`` unwarm each window *within* a step by design, so they
re-pay windowed warms every step — their per-step ``pool_provisioning`` shows it.

Metrics per step:
  * headline — ``env_ready_all_s``: step start -> the LAST task's environment
    delivered (its process_fn invoked, i.e. claim complete), the sync-barrier
    quantity; ``step_wall_s``; and ``ttfe_*`` (step/task completion latency).
  * secondary — claims issued per step, reset costs (recycle/hold arms), open
    sockets / FDs / RSS, plus per-task env-delivery distributions.
"""
from __future__ import annotations
import asyncio, json, os, sys, threading, time

import bench_core
from agent_sandbox_rl import (AsyncSandboxFleet, FleetConfig, Task)
try:
    from agent_sandbox_rl import GitRestoreReset
except ImportError:
    GitRestoreReset = None
from agent_sandbox_rl.sources import ListSource


def _pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


class _StepProbe:
    """Wraps process_fn: timestamps env delivery (call start = claim complete) and
    task completion, relative to step start, plus per-task TTFE and paired TTFI."""

    def __init__(self, probe: str, timeout: float | None = None):
        self.probe = probe
        self.timeout = timeout
        self.step_t0 = 0.0
        self._lock = threading.Lock()
        self._acq_times: dict[int, tuple[float, float]] = {}
        self.env_delivered: list[float] = []
        self.task_done: list[float] = []
        self.exec_durs: list[float] = []
        self.paired_wire_s: list[float] = []
        self.paired_ttfi_s: list[float] = []

    def record_acquire(self, handle, ts: float, te: float) -> None:
        with self._lock:
            self._acq_times[id(handle)] = (ts, te)

    def begin_step(self):
        with self._lock:
            self.step_t0 = time.monotonic()
            self._acq_times.clear()
            self.env_delivered = []
            self.task_done = []
            self.exec_durs = []
            self.paired_wire_s = []
            self.paired_ttfi_s = []

    def __call__(self, task: Task, handle):
        t_deliv = time.monotonic()
        with self._lock:
            step_t0 = self.step_t0
            self.env_delivered.append(t_deliv - step_t0)
            acq_pair = self._acq_times.pop(id(handle), None)
        out = bench_core.exec_checked(handle, self.probe, timeout=self.timeout)
        t_done = time.monotonic()
        with self._lock:
            self.task_done.append(t_done - step_t0)
            self.exec_durs.append(t_done - t_deliv)
            if acq_pair is not None:
                ts, te = acq_pair
                self.paired_wire_s.append(te - ts)
                self.paired_ttfi_s.append(t_done - ts)
        return out

    def step_metrics(self):
        with self._lock:
            env = list(self.env_delivered)
            done = list(self.task_done)
            execs = list(self.exec_durs)
            p_wire = list(self.paired_wire_s)
            p_ttfi = list(self.paired_ttfi_s)
        return {
            "env_ready_all_s": round(max(env), 2) if env else None,
            "env_ready_avg_s": round(sum(env) / len(env), 2) if env else None,
            "env_ready_p50_s": round(_pct(env, 0.50), 2) if env else None,
            "env_ready_p90_s": round(_pct(env, 0.90), 2) if env else None,
            "env_ready_p99_s": round(_pct(env, 0.99), 2) if env else None,
            "task_done_all_s": round(max(done), 2) if done else None,
            "ttfe_avg_s": round(sum(done) / len(done), 2) if done else None,
            "ttfe_p50_s": round(_pct(done, 0.50), 2) if done else None,
            "ttfe_p90_s": round(_pct(done, 0.90), 2) if done else None,
            "ttfe_max_s": round(max(done), 2) if done else None,
            "first_cmd_exec_s": bench_core.quantiles(execs),
            "paired_wire_claim_s": bench_core.quantiles(p_wire),
            "paired_ttfi_s": bench_core.quantiles(p_ttfi),
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


def _fleet_cfg(a, sel, rc) -> FleetConfig:
    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)
    tspec = bench_core.build_template_spec(a, sel=sel, rc=rc)
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
    bench_core.apply_claim_mode_kwargs(cfg_kwargs, a)
    return FleetConfig(**cfg_kwargs)


async def _bounded_gather(n, coros):
    sem = asyncio.Semaphore(max(1, n))

    async def _guard(c):
        async with sem:
            return await c

    return await asyncio.gather(*(_guard(c) for c in coros), return_exceptions=True)


class _MissingBaselineOutcome:
    clean, seconds, reason = False, 0.0, "prime_failed"


async def _missing_baseline_reset():
    return _MissingBaselineOutcome()


async def _run_hold_steps(images, tasks, a, sel, rc):
    if GitRestoreReset is None:
        raise NotImplementedError("hold requires GitRestoreReset in SDK")
    cfg = _fleet_cfg(a, sel, rc)
    n_clusters = max(1, len(cfg.clusters))
    af = AsyncSandboxFleet(cfg)
    G = max(1, a.rollouts)
    img_tasks = [Task(id=bench_core._sid(img), image=img) for img in images]
    af.load_tasks(ListSource(img_tasks))
    reset = GitRestoreReset(testbed=a.testbed)
    probe = _StepProbe(a.probe, timeout=a.exec_timeout)
    exec_conc = a.exec_concurrency or a.concurrency
    label = "hold"
    mif = "n/a"
    if getattr(cfg, "batch_claims", None):
        mif = getattr(cfg.batch_claims, "max_in_flight", "default(20)")
    print(f"[{label}] mode={getattr(a, 'claim_mode', 'per_claim')}(mif={mif}) "
          f"{len(images)} images (held) x {G} rollouts x {a.steps} steps; "
          f"conc={a.concurrency} exec_conc={exec_conc} dwell={a.step_dwell}s", flush=True)

    def _reset_one(handle, base):
        return reset.reset(handle, base, timeout=a.reset_timeout)

    async def _reset_and_replace(step):
        outcomes = await _bounded_gather(
            a.concurrency,
            [af._to_thread(_reset_one, h, baselines[id(h)])
             if baselines.get(id(h)) is not None else _missing_baseline_reset()
             for h in handles])
        secs, repl = [], 0
        for i, (h, oc) in enumerate(zip(list(handles), outcomes)):
            clean = (not isinstance(oc, Exception)) and getattr(oc, "clean", False)
            if not isinstance(oc, Exception) and oc is not None and getattr(oc, "reason", "") != "prime_failed":
                secs.append(oc.seconds)
            if not clean:
                try:
                    await af.release(h)
                except Exception as rel_exc:
                    errors.append(f"s{step} release failed ({h.claim_name}): {type(rel_exc).__name__}: {rel_exc}")
                try:
                    nh = await af.acquire(img_tasks[i])
                    b_new = await af._to_thread(reset.prime, nh)
                    baselines[id(nh)] = b_new
                    handles[i] = nh
                    repl += 1
                except Exception as e:
                    baselines.pop(id(h), None)
                    errors.append(f"s{step} replace failed: {type(e).__name__}: {e}")
        return secs, repl

    steps_out, errors = [], []
    proc_before = bench_core.proc_stats()
    variant_t0 = time.monotonic()
    handles, baselines = [], {}
    verify_out = None
    teardown_s = None
    try:
        await af.setup()
        t_setup = time.monotonic()
        handles = await af.acquire_batch(img_tasks)
        acquire_all_s = time.monotonic() - t_setup
        prime_res = await _bounded_gather(
            a.claim_concurrency,
            [af._to_thread(reset.prime, h) for h in handles])
        for h, b in zip(handles, prime_res):
            if isinstance(b, Exception):
                baselines[id(h)] = None
                errors.append(f"setup prime failed ({h.claim_name}): {type(b).__name__}: {b}")
            else:
                baselines[id(h)] = b
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
        t_td = time.monotonic()
        await af.release_all()
        await af.teardown()
        teardown_s = round(time.monotonic() - t_td, 2)
        verify_out = await bench_core.safe_verify_teardown(af)
    finally:
        bench_core.close_fleet(af, wait=False)

    batch_mif = None
    if getattr(cfg, "batch_claims", None):
        batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)
    variant_wall = round(time.monotonic() - variant_t0, 2)
    tot_ok = sum(s["ok"] for s in steps_out)
    tot_tasks = sum(s["tasks"] for s in steps_out)
    return {
        "strategy": "hold",
        "recycle": True,
        "label": label,
        "clusters": n_clusters,
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "batch_max_in_flight": batch_mif,
        "variant_wall_s": variant_wall,
        "teardown_s": teardown_s,
        "total_ok": tot_ok,
        "total_tasks": tot_tasks,
        "proc_before": proc_before,
        "proc_after": proc_after_hold if (proc_after_hold := bench_core.proc_stats()) else {},
        "verify_teardown": verify_out,
        "steps": steps_out,
        "errors_sample": errors[:8],
    }


async def _run_variant_steps(strategy, recycle, images, tasks, a, sel, rc):
    cfg = _fleet_cfg(a, sel, rc)
    n_clusters = max(1, len(cfg.clusters))
    af = AsyncSandboxFleet(cfg)
    af.load_tasks(ListSource(tasks))
    label = f"{strategy}{'+recycle' if recycle else ''}"
    probe = _StepProbe(a.probe, timeout=a.exec_timeout)

    orig_acquire = af._fleet.acquire

    def _timed_acquire(task, *args, **kwargs):
        ts = time.monotonic()
        h = orig_acquire(task, *args, **kwargs)
        te = time.monotonic()
        probe.record_acquire(h, ts, te)
        return h

    af._fleet.acquire = _timed_acquire  # type: ignore[method-assign]
    mif = "n/a"
    if getattr(cfg, "batch_claims", None):
        mif = getattr(cfg.batch_claims, "max_in_flight", "default(20)")
    print(f"[{label}] mode={getattr(a, 'claim_mode', 'per_claim')}(mif={mif}) "
          f"{len(images)} images x {a.rollouts} x {a.steps} steps; "
          f"conc={a.concurrency} warm_per_task={a.warm_per_task} max_wp={a.max_warmpool_size}",
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
            run_kwargs = dict(
                strategy=strategy,
                concurrency=a.concurrency,
                keep_warm=not last,
                recycle=recycle,
                max_reuses=a.max_reuses,
                reset_timeout=a.reset_timeout,
                shards_per_image=getattr(a, "shards_per_image", 1),
                claim_concurrency=a.claim_concurrency,
            )
            if recycle:
                if GitRestoreReset is None:
                    raise NotImplementedError("recycle=True requires GitRestoreReset in SDK")
                run_kwargs["reset"] = GitRestoreReset(testbed=a.testbed)
            results = await af.run(probe, **run_kwargs)
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
            step_goodput = round(ok / wall, 3) if wall > 0 else None
            s = {
                "step": step,
                "step_wall_s": round(wall, 2),
                **probe.step_metrics(),
                "tasks": tot,
                "ok": ok,
                "err": err,
                "goodput_ok_per_s": step_goodput,
                "per_cluster_goodput_ok_per_s": round(step_goodput / n_clusters, 3) if step_goodput is not None else None,
                "claims": rep.get("claims"),
                "pool_provisioning": _pool_provisioning(ph),
                "claim": ph.get("claim"),
                "claim_err": ph_err.get("claim"),
                "process": ph.get("process"),
                "release": ph.get("release"),
                "reset": ph.get("reset"),
                "teardown": ph.get("teardown"),
                "phases_err": ph_err,
                "proc": ps,
            }
            steps_out.append(s)
            print(f"  STEP {step}: wall={s['step_wall_s']}s "
                  f"env_ready_all={s['env_ready_all_s']}s p50={s['env_ready_p50_s']}s "
                  f"p99={s['env_ready_p99_s']}s claims={s['claims']} "
                  f"ok={ok}/{tot} sockets={ps['open_sockets']} rss={ps['vm_rss_mb']}MB",
                  flush=True)
        proc_after_var = bench_core.proc_stats()
        verify_out = await bench_core.safe_verify_teardown(af)
    except BaseException:
        try:
            await af.release_all()
            await af.teardown()
        except Exception as td_err:
            print(f"[{label}] teardown after failure ALSO failed: {td_err}", flush=True)
        raise
    finally:
        bench_core.close_fleet(af, wait=False)

    batch_mif = None
    if getattr(cfg, "batch_claims", None):
        batch_mif = getattr(cfg.batch_claims, "max_in_flight", None)
    variant_wall = round(time.monotonic() - variant_t0, 2)
    tot_ok = sum(s["ok"] for s in steps_out)
    tot_tasks = sum(s["tasks"] for s in steps_out)
    var_goodput = round(tot_ok / variant_wall, 3) if variant_wall > 0 else None
    return {
        "strategy": strategy,
        "recycle": recycle,
        "label": label,
        "clusters": n_clusters,
        "claim_mode": getattr(a, "claim_mode", "per_claim"),
        "batch_max_in_flight": batch_mif,
        "variant_wall_s": variant_wall,
        "total_ok": tot_ok,
        "total_tasks": tot_tasks,
        "claim_success_rate": round(tot_ok / tot_tasks, 5) if tot_tasks > 0 else None,
        "goodput_ok_per_s": var_goodput,
        "per_cluster_goodput_ok_per_s": round(var_goodput / n_clusters, 3) if var_goodput is not None else None,
        "wall_per_ok_task_s": round(variant_wall / tot_ok, 4) if tot_ok > 0 else None,
        "proc_before": proc_before,
        "proc_after": proc_after_var,
        "verify_teardown": verify_out,
        "steps": steps_out,
        "errors_sample": errors[:8],
    }


async def _main(a):
    if not getattr(a, "in_cluster", False) and not a.context and not getattr(a, "contexts", None):
        raise SystemExit("one of --context, --contexts, or --in-cluster is required")
    images = [l.strip() for l in open(a.images_file) if l.strip()][:a.problems]
    tasks = [Task(id=f"{bench_core._sid(i)}-r{g}", image=i)
             for i in images for g in range(a.rollouts)]
    sel = bench_core._parse_selector(a.node_selector)
    rc = a.runtime_class or None
    clusters = bench_core.build_clusters(a, sel=sel, rc=rc)

    if a.hold:
        print(f"step sweep: hold variant, {a.steps} steps (claim_mode={getattr(a, 'claim_mode', 'per_claim')})",
              flush=True)
        out_variants = [await _run_hold_steps(images, tasks, a, sel, rc)]
    else:
        variants = bench_core._variants(a.strategies, a.recycle, getattr(a, "recycle_only", False))
        print(f"step sweep: {len(variants)} variant(s), {a.steps} steps each "
              f"(claim_mode={getattr(a, 'claim_mode', 'per_claim')})",
              flush=True)
        out_variants = []
        for idx, (strategy, recycle) in enumerate(variants):
            if idx > 0:
                drain_info = await bench_core.wait_clusters_drained(clusters, timeout=240.0, settle_s=30.0)
                print(f"  [inter-variant drain] {drain_info}", flush=True)
            out_variants.append(
                await _run_variant_steps(strategy, recycle, images, tasks, a, sel, rc))

    out = {
        "params": {
            **{k: v for k, v in vars(a).items() if k != "_driver"},
            "driver": "run_step_bench",
            "images": len(images),
            "tasks_per_step": len(tasks),
            "node_selector_parsed": sel,
        },
        "variants": out_variants,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
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
    p.add_argument("--shards-per-image", type=int, default=1,
                   help="recycle: K sandboxes per image running chains in parallel")
    p.add_argument("--hold", action="store_true",
                   help="hold-across-steps: acquire every environment once, exec per step, git-restore between steps")
    p.add_argument("--step-dwell", type=float, default=0.0,
                   help="hold: seconds of simulated trainer-update window per step")
    p.add_argument("--exec-concurrency", type=int, default=0,
                   help="hold: cap on simultaneous exec streams (0 = --concurrency)")
    p.add_argument("--exec-timeout", type=float, default=120.0,
                   help="per-exec timeout (s)")
    a = p.parse_args()
    asyncio.run(_main(a))


if __name__ == "__main__":
    main()
