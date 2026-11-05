"""B0 baseline: one raw pod per task — the r2egym/tunix-deepswe execution path.

Replicates the pod lifecycle of R2E-Gym's ``DockerRuntime`` ``kubernetes`` backend
(the path tunix deepswe uses today): cold-create a pod per task, wait for Ready,
exec the probe, delete. No warm pools, no claims, no controller — this is the
"before" arm for the SDK strategy comparison (see plans/blog-docker-vs-sdk-eval500.md
in the notes repo).

Fidelity: the pod body mirrors ``DockerRuntime._start_kubernetes_pod`` (restartPolicy
Never, single container, ``/bin/sh -c`` command, stdin/tty, env PATH, resource
*requests*). Cluster-specific fields from r2egym (their Karpenter nodeSelector and
``dockerhub-pro`` pull secret) are parameterized, exactly as deepswe's own
nodeSelector monkeypatch does. ``DockerRuntime`` itself is not imported: it requires
full dataset rows and runs ``setup_env()`` execs that would pollute infra timing.

Parity notes for the report:
  * default requests are 125m/1Gi to match the SDK arms; ``--authentic-resources``
    restores r2egym's 1 CPU / 1Gi (at which 500-wide does not even fit a 440-vCPU
    pool -- report that, don't run it wide).
  * ``--runtime-class gvisor`` for the isolation-parity series (primary).
  * ``--runtime-class "" --tolerate-sandbox-taint`` for the B0-on-runc appendix
    arm (D1): runc pods scheduled onto the same tainted GKE Sandbox pool, so the
    only variable vs the primary series is the per-pod runtime.

Usage:
  python run_docker_baseline.py \
      --context <ctx> --namespace default \
      --node-selector cloud.google.com/gke-nodepool=gvisor-c3-44 \
      --runtime-class gvisor \
      --images-file swebench500_images.txt --concurrency 500 \
      --out results/b0_baseline.json

Step-loop mode (blog step benchmark): ``--steps N --rollouts G`` runs N
synchronous passes of (images x G) with a barrier between passes; per-step
summaries land in the JSON ``steps`` array. NB with --steps > 1 the top-level
``summary.wall_pure_s`` spans barrier idle time -- quote per-step metrics, not
the single-batch summary fields, for multi-step runs.
"""
from __future__ import annotations
import argparse, concurrent.futures, json, random, signal, statistics, string, sys
import threading, time

from kubernetes import client, config
from kubernetes.stream import stream as k8s_stream

DOCKER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
HOLD_CMD = "sleep infinity"          # r2egym holds the container open; probe execs in
READY_TIMEOUT = 900.0
_tls = threading.local()


def parse_args():
  p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  p.add_argument("--context", default=None,
                 help="kubeconfig context (laptop driver); omit with --in-cluster")
  p.add_argument("--in-cluster", action="store_true",
                 help="load in-cluster config from the pod ServiceAccount")
  p.add_argument("--namespace", default="default")     # r2egym hardcodes 'default'
  p.add_argument("--node-selector", default=None, help="key=value")
  p.add_argument("--runtime-class", default="", help="'gvisor' for parity series")
  p.add_argument("--tolerate-sandbox-taint", action="store_true",
                 help="tolerate the GKE Sandbox node taint without requesting the "
                      "gvisor RuntimeClass -- the B0-on-runc appendix arm (D1): "
                      "runc pods on the same tainted gVisor pool")
  p.add_argument("--images-file", required=True)
  p.add_argument("--limit", type=int, default=None)
  p.add_argument("--steps", type=int, default=1,
                 help="synchronous training steps: each step runs every task and "
                      "BARRIERS (step k+1 starts only after all step-k tasks "
                      "complete). A fresh pod per task per step -- the deepswe loop")
  p.add_argument("--rollouts", type=int, default=1,
                 help="tasks per image within each step (G)")
  p.add_argument("--concurrency", type=int, default=500)
  p.add_argument("--cpu", default="125m")
  p.add_argument("--memory", default="1Gi")
  p.add_argument("--authentic-resources", action="store_true",
                 help="use r2egym's default requests (cpu=1, memory=1Gi)")
  p.add_argument("--probe", default="true", help="command exec'd per task (no-op default)")
  p.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT)
  p.add_argument("--out", default="results/b0_baseline.json")
  return p.parse_args()


def core_api() -> client.CoreV1Api:
  if getattr(_tls, "api", None) is None:
    _tls.api = client.CoreV1Api()
  return _tls.api


def pod_body(name, image, a, run_id):
  # Mirrors r2egym DockerRuntime._start_kubernetes_pod pod_body; cluster-specific
  # fields (nodeSelector / pull secret / runtimeClass / tolerations) parameterized.
  cpu = "1" if a.authentic_resources else a.cpu
  spec = {
      "restartPolicy": "Never",
      "containers": [{
          "name": name,
          "image": image,
          "command": ["/bin/sh", "-c"],
          "args": [HOLD_CMD],
          "stdin": True,
          "tty": True,
          "env": [{"name": "PATH", "value": DOCKER_PATH}],
          "resources": {"requests": {"cpu": cpu, "memory": a.memory}},
      }],
  }
  if a.node_selector:
    k, v = a.node_selector.split("=", 1)
    spec["nodeSelector"] = {k: v}
  if a.runtime_class:
    spec["runtimeClassName"] = a.runtime_class
  if a.runtime_class or a.tolerate_sandbox_taint:
    spec["tolerations"] = [{"key": "sandbox.gke.io/runtime",
                            "operator": "Equal", "value": "gvisor",
                            "effect": "NoSchedule"}]
  return {"apiVersion": "v1", "kind": "Pod",
          "metadata": {"name": name, "labels": {"bench-run": run_id,
                                                "bench-arm": "b0-raw-pod"}},
          "spec": spec}


def iso_ts(t):
  return t.timestamp() if t else None


def run_task(step, idx, image, a, run_id):
  api = core_api()
  name = f"b0-{run_id}-s{step}-{idx:04d}"
  rec = {"step": step, "idx": idx, "image": image, "pod": name, "ok": False,
         "error": None}
  t0 = time.time()
  rec["t_create"] = t0
  try:
    api.create_namespaced_pod(namespace=a.namespace,
                              body=pod_body(name, image, a, run_id))
    rec["create_s"] = time.time() - t0
    # Poll until Ready (same observable milestone r2egym waits for).
    deadline = t0 + a.ready_timeout
    pod = None
    while time.time() < deadline:
      pod = api.read_namespaced_pod(name=name, namespace=a.namespace)
      phase = pod.status.phase
      conds = {c.type: c for c in (pod.status.conditions or [])}
      if phase == "Failed":
        raise RuntimeError(f"pod Failed: {pod.status.reason}")
      ready = conds.get("Ready")
      if ready is not None and ready.status == "True":
        break
      time.sleep(0.5)
    else:
      raise TimeoutError(f"pod not Ready in {a.ready_timeout}s")
    rec["ready_s"] = time.time() - t0
    conds = {c.type: c for c in (pod.status.conditions or [])}
    cs = (pod.status.container_statuses or [None])[0]
    rec["scheduled_at"] = iso_ts(getattr(conds.get("PodScheduled"), "last_transition_time", None))
    rec["started_at"] = iso_ts(cs.state.running.started_at) if cs and cs.state.running else None
    if rec["scheduled_at"] and rec["started_at"]:
      rec["image_start_s"] = rec["started_at"] - rec["scheduled_at"]  # pull+create+start
    # Exec the probe -- TTFE endpoint. exec_start_s is the env-DELIVERED
    # timestamp (pod usable, probe about to run): per-step barrier metrics use
    # it so a long real probe is never counted as provisioning (symmetric with
    # the SDK runner, which stamps env delivery at claim-complete).
    rec["exec_start_s"] = time.time() - t0
    out = k8s_stream(api.connect_get_namespaced_pod_exec, name, a.namespace,
                     command=["/bin/sh", "-c", a.probe],
                     stderr=True, stdin=False, stdout=True, tty=False)
    rec["ttfe_s"] = time.time() - t0
    rec["probe_out"] = (out or "").strip()[:80]
    rec["ok"] = True
  except Exception as e:                                   # noqa: BLE001
    rec["error"] = f"{type(e).__name__}: {e}"[:300]
    rec["ttfe_s"] = None
  finally:
    t_del = time.time()
    try:  # release semantics: issue delete, don't wait for full termination
      api.delete_namespaced_pod(name=name, namespace=a.namespace,
                                grace_period_seconds=0)
    except Exception:                                      # noqa: BLE001
      pass
    rec["delete_issue_s"] = time.time() - t_del
    rec["total_s"] = time.time() - t0
  return rec


def pct(vals, q):
  if not vals:
    return None
  s = sorted(vals)
  return s[min(len(s) - 1, int(len(s) * q))]


def step_summary(step, recs, step_t0, step_wall):
  """Per-step metrics. env_ready_all_s is the sync-barrier headline: step start ->
  the LAST task's env DELIVERED (exec-start, i.e. pod usable and probe about to
  run) -- what a synchronous training step waits on before its work can proceed.
  Deliberately excludes probe runtime, symmetric with the SDK runner's
  claim-complete stamp; ttfe_* fields keep the campaign's exec-return convention.
  The barrier is on exec COMPLETION (step k+1 starts only then); prior-step pods
  may still be Terminating -- faithful to the deepswe delete-and-move-on loop."""
  ok = [r for r in recs if r["ok"]]
  ttfe = [r["ttfe_s"] for r in ok]
  imgst = [r["image_start_s"] for r in ok if r.get("image_start_s")]
  ends = [r["t_create"] + r.get("exec_start_s", r["ttfe_s"]) for r in ok]
  return {
      "step": step,
      "step_wall_s": round(step_wall, 1),
      "env_ready_all_s": round(max(ends) - step_t0, 1) if ends else None,
      "tasks": len(recs), "ok": len(ok), "err": len(recs) - len(ok),
      "ttfe_avg_s": round(statistics.mean(ttfe), 2) if ttfe else None,
      "ttfe_p50_s": round(pct(ttfe, 0.50), 2) if ttfe else None,
      "ttfe_p90_s": round(pct(ttfe, 0.90), 2) if ttfe else None,
      "ttfe_max_s": round(max(ttfe), 2) if ttfe else None,
      "image_start_avg_s": round(statistics.mean(imgst), 2) if imgst else None,
      "image_start_p90_s": round(pct(imgst, 0.90), 2) if imgst else None,
      "image_start_max_s": round(max(imgst), 2) if imgst else None,
  }


def main():
  a = parse_args()
  if a.in_cluster:
    config.load_incluster_config()
  elif a.context:
    config.load_kube_config(context=a.context)
  else:
    raise SystemExit("one of --context (laptop) or --in-cluster (pod) is required")
  images = [l.strip() for l in open(a.images_file)
            if l.strip() and not l.startswith("#")]
  if a.limit:
    images = images[:a.limit]
  run_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
  step_tasks = [(g * len(images) + i, img)
                for g in range(a.rollouts) for i, img in enumerate(images)]
  print(f"B0 raw-pod baseline: {len(images)} images x {a.rollouts} rollouts x "
        f"{a.steps} steps = {len(step_tasks) * a.steps} tasks, conc={a.concurrency} "
        f"runtime_class={a.runtime_class or 'runc'} ns={a.namespace} run={run_id}",
        flush=True)

  def sweep():  # delete anything left over from this run
    try:
      core_api().delete_collection_namespaced_pod(
          namespace=a.namespace, label_selector=f"bench-run={run_id}",
          grace_period_seconds=0)
    except Exception:                                      # noqa: BLE001
      pass
  signal.signal(signal.SIGINT, lambda *_: (sweep(), sys.exit(130)))
  signal.signal(signal.SIGTERM, lambda *_: (sweep(), sys.exit(143)))

  wall0 = time.time()
  results = []
  steps_out = []
  with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
    for step in range(a.steps):
      # SYNC BARRIER: all of step k completes before step k+1 submits anything.
      step_t0 = time.time()
      done = 0
      step_recs = []
      futs = [ex.submit(run_task, step, i, img, a, run_id)
              for i, img in step_tasks]
      for f in concurrent.futures.as_completed(futs):
        step_recs.append(f.result())
        done += 1
        if done % 25 == 0 or done == len(futs):
          print(f"  step {step}: {done}/{len(futs)} done "
                f"({time.time()-step_t0:.0f}s)", flush=True)
      s = step_summary(step, step_recs, step_t0, time.time() - step_t0)
      print(f"  STEP {step}: wall={s['step_wall_s']}s "
            f"env_ready_all={s['env_ready_all_s']}s "
            f"ttfe avg/max={s['ttfe_avg_s']}/{s['ttfe_max_s']}s "
            f"ok={s['ok']}/{s['tasks']}", flush=True)
      steps_out.append(s)
      results.extend(step_recs)
  wall = time.time() - wall0

  # Symmetric teardown: SDK arms wait for full fleet teardown, so B0 times
  # delete-collection until zero pods with our label remain.
  t_td = time.time()
  sweep()
  api = core_api()
  while True:
    n = len(api.list_namespaced_pod(namespace=a.namespace,
            label_selector=f"bench-run={run_id}").items)
    if n == 0 or time.time() - t_td > 600:
      break
    time.sleep(2)
  teardown_s = time.time() - t_td

  ok = [r for r in results if r["ok"]]
  ttfe = [r["ttfe_s"] for r in ok]
  ready = [r["ready_s"] for r in ok if r.get("ready_s")]
  imgst = [r["image_start_s"] for r in ok if r.get("image_start_s")]
  # wall_pure: first create -> last exec-done (no setup exists in B0; excludes deletes)
  ends = [r["t_create"] + r["ttfe_s"] for r in ok]
  wall_pure = (max(ends) - min(r["t_create"] for r in ok)) if ok else None
  summary = {
      "arm": "b0-raw-pod",
      "wall_e2e_s": round(wall + teardown_s, 1),   # incl timed teardown-to-zero
      "wall_s": round(wall, 1),                    # task loop only (legacy field)
      "wall_pure_s": round(wall_pure, 1) if wall_pure else None,
      "teardown_s": round(teardown_s, 1),
      "tasks": len(results), "ok": len(ok), "err": len(results) - len(ok),
      "ttfe_avg_s": round(statistics.mean(ttfe), 2) if ttfe else None,
      "ttfe_p50_s": round(pct(ttfe, 0.50), 2) if ttfe else None,
      "ttfe_p90_s": round(pct(ttfe, 0.90), 2) if ttfe else None,
      "ttfe_max_s": round(max(ttfe), 2) if ttfe else None,
      "ready_avg_s": round(statistics.mean(ready), 2) if ready else None,
      "ready_max_s": round(max(ready), 2) if ready else None,
      "image_start_avg_s": round(statistics.mean(imgst), 2) if imgst else None,
      "image_start_p90_s": round(pct(imgst, 0.90), 2) if imgst else None,
      "image_start_max_s": round(max(imgst), 2) if imgst else None,
  }
  out = {"params": {**vars(a), "images": len(images), "run_id": run_id,
                    "pod_spec_fidelity": "r2egym DockerRuntime._start_kubernetes_pod"},
         "summary": summary, "steps": steps_out, "tasks": results}
  with open(a.out, "w") as fh:
    json.dump(out, fh, indent=1)
  print(json.dumps(summary, indent=2))
  print(f"-> {a.out}")


if __name__ == "__main__":
  main()
