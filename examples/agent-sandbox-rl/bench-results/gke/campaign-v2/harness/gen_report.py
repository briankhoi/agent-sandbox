#!/usr/bin/env python3
"""Render a comprehensive per-run benchmark report from performance_reports/
REPORT_TEMPLATE.md and print/append the RESULTS.md row.

The results JSON is a **sweep**: one entry per variant (a warm-pool strategy, with
recycle optionally layered on). This renders a per-variant comparison table + phase
breakdown, and appends one RESULTS.md row per variant.

Beyond the run's numbers (from the results JSON), this **live-queries the cluster**
at report time (kubectl + gcloud) to capture the full environment the way the
reference reports do: k8s version, controller image + concurrency flags, the pool's
machine/disk/zones, per-node allocatable (vCPU/mem/ephemeral/pods), pool totals,
taint, image streaming, secondary boot disk. Nodes/controller persist after the run,
so this works post-teardown.

Usage:
  python drivers/gen_report.py results/basic_swebench_3000.json --tier T0 \
    [--daemon "stock"] [--note "..."] [--append] [--no-cluster-info]
Context/namespace/pool are read from the results JSON; most env fields are
auto-collected and can be overridden with flags (--gke/--sbd/--imgstream/--ctrl/…).
"""
import argparse, collections, datetime, json, os, re, subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(REPO, "performance_reports", "REPORT_TEMPLATE.md")
REPORTS = os.path.join(REPO, "performance_reports")
RESULTS = os.path.join(REPO, "RESULTS.md")


def default_user():
    try:
        u = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
        if u:
            return u
    except Exception:
        pass
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-").lower() or "run"


def _next_row_num(path):
    """Next value for RESULTS.md's leading '#' column = max existing + 1 (so appended
    rows stay numbered consistently with the perf-at-a-glance chart). Defaults to 1."""
    mx = 0
    try:
        for ln in open(path):
            m = re.match(r"\|\s*(\d+)\s*\|", ln)
            if m:
                mx = max(mx, int(m.group(1)))
    except Exception:
        pass
    return mx + 1


def _sh(args, timeout=60):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _gib(s):
    if not s:
        return "?"
    try:
        if s.endswith("Ki"):
            return "%.0fGi" % (int(s[:-2]) / 2**20)
        if s.endswith("Mi"):
            return "%.0fGi" % (int(s[:-2]) / 2**10)
        if s.endswith("Gi"):
            return s
        if str(s).isdigit():                       # plain bytes (GKE ephemeral-storage)
            return "%.0fGi" % (int(s) / 2**30)
        return s
    except Exception:
        return s


def _cpu(c):
    try:
        return int(c[:-1]) / 1000 if c.endswith("m") else float(c)
    except Exception:
        return 0.0


def _pool_facts(ctx, pool, proj, loc, clus):
    """Live spec for one node pool: node count, machine, zones, per-node allocatable
    (vCPU/mem/ephemeral/pods), taint, totals, disk, SBD, image streaming. Best-effort
    — missing pieces are simply absent. Used for both the sandbox and controller pools."""
    f = {}
    nodesj = _sh(["kubectl", "--context", ctx, "get", "nodes", "-l",
                  f"cloud.google.com/gke-nodepool={pool}", "-o", "json"])
    try:
        items = json.loads(nodesj)["items"]
        f["nodes"] = len(items)
        if items:
            n0 = items[0]; al = n0["status"]["allocatable"]; lab = n0["metadata"]["labels"]
            f["machine"] = lab.get("node.kubernetes.io/instance-type", "?")
            f["zones"] = ",".join(sorted({i["metadata"]["labels"].get(
                "topology.kubernetes.io/zone", "?") for i in items}))
            f["vcpu"] = "%.1f" % _cpu(al.get("cpu", "0"))
            f["mem"] = _gib(al.get("memory"))
            f["eph"] = _gib(al.get("ephemeral-storage"))
            f["pods"] = al.get("pods", "?")
            taints = n0["spec"].get("taints", [])
            f["taint"] = ";".join(f"{t['key']}={t.get('value','')}:{t['effect']}"
                                  for t in taints) or "none"
            f["total_vcpu"] = "%.0f" % sum(_cpu(i["status"]["allocatable"].get("cpu", "0")) for i in items)
            f["total_pods"] = sum(int(i["status"]["allocatable"].get("pods", "0")) for i in items)
    except Exception:
        pass
    g = _sh(["gcloud", "container", "node-pools", "describe", pool, "--cluster", clus,
             "--project", proj, "--region", loc,
             "--format=value(config.diskType,config.diskSizeGb,config.secondaryBootDisks,config.gcfsConfig.enabled)"])
    if g:
        p = g.split("\t")
        if len(p) >= 2:
            f["disk"] = f"{p[0]} {p[1]}GB"
        f["sbd"] = "present" if (len(p) > 2 and p[2] and p[2] != "None") else "none"
        f["imgstream"] = "on" if (len(p) > 3 and p[3] == "True") else "off"
    return f


def collect_cluster(ctx, pool, ns):
    """Best-effort live cluster facts. Missing pieces stay '?'; never raises."""
    d = collections.defaultdict(lambda: "?")
    m = re.match(r"gke_([^_]+)_([^_]+)_(.+)", ctx or "")
    proj, loc, clus = (m.group(1), m.group(2), m.group(3)) if m else ("?", "?", "?")
    d["project"], d["region"], d["cluster_name"] = proj, loc, clus

    v = _sh(["kubectl", "--context", ctx, "version", "-o", "json"])
    try:
        d["k8s"] = json.loads(v).get("serverVersion", {}).get("gitVersion", "?")
    except Exception:
        pass

    depl = _sh(["kubectl", "--context", ctx, "-n", "agent-sandbox-system", "get",
                "deploy", "agent-sandbox-controller", "-o", "json"])
    try:
        c = json.loads(depl)["spec"]["template"]["spec"]["containers"][0]
        d["ctrl_image"] = c.get("image", "?")
        args = c.get("args", [])
        d["ctrl_flags"] = " ".join(a for a in args if any(
            k in a for k in ("concurrent-workers", "batch-size", "kube-api-qps", "kube-api-burst")))
        sbx = next((a.split("=")[1] for a in args if "--sandbox-concurrent-workers=" in a), "?")
        wp = next((a.split("=")[1] for a in args if "--sandbox-warm-pool-concurrent-workers=" in a), "?")
        d["ctrl_workers"] = f"{sbx}/{wp}"
    except Exception:
        pass

    # Controller node instance type — the controller runs on its own pool (not the
    # sandbox/gvisor pool), and its size affects reconcile throughput, so record it.
    try:
        pods = json.loads(_sh(["kubectl", "--context", ctx, "-n", "agent-sandbox-system",
                               "get", "pods", "-o", "json"]))["items"]
        cnode = next((p["spec"].get("nodeName") for p in pods
                      if "controller" in p["metadata"]["name"]), None) \
            or (pods[0]["spec"].get("nodeName") if pods else None)
        if cnode:
            clab = json.loads(_sh(["kubectl", "--context", ctx, "get", "node", cnode,
                                   "-o", "json"]))["metadata"]["labels"]
            d["ctrl_machine"] = clab.get("node.kubernetes.io/instance-type", "?")
            d["ctrl_pool"] = clab.get("cloud.google.com/gke-nodepool", "?")
    except Exception:
        pass

    # Sandbox (gvisor) pool — where the benchmark's sandboxes run.
    sf = _pool_facts(ctx, pool, proj, loc, clus)
    d["pool_nodes"] = sf.get("nodes", "?"); d["machine"] = sf.get("machine", "?")
    d["zones"] = sf.get("zones", "?"); d["node_vcpu"] = sf.get("vcpu", "?")
    d["node_mem"] = sf.get("mem", "?"); d["node_eph"] = sf.get("eph", "?")
    d["node_pods"] = sf.get("pods", "?"); d["taint"] = sf.get("taint", "?")
    d["total_vcpu"] = sf.get("total_vcpu", "?"); d["total_pods"] = sf.get("total_pods", "?")
    d["disk"] = sf.get("disk", "?"); d["sbd"] = sf.get("sbd", "?"); d["imgstream"] = sf.get("imgstream", "?")

    # Controller pool — full spec of the pool the controller runs on (d["ctrl_pool"]
    # was set from the controller pod's node above).
    if d.get("ctrl_pool") not in (None, "?"):
        cf = _pool_facts(ctx, d["ctrl_pool"], proj, loc, clus)
        d["ctrl_nodes"] = cf.get("nodes", "?"); d["ctrl_machine"] = cf.get("machine", d.get("ctrl_machine", "?"))
        d["ctrl_zones"] = cf.get("zones", "?"); d["ctrl_vcpu"] = cf.get("vcpu", "?")
        d["ctrl_mem"] = cf.get("mem", "?"); d["ctrl_eph"] = cf.get("eph", "?")
        d["ctrl_node_pods"] = cf.get("pods", "?"); d["ctrl_taint"] = cf.get("taint", "?")
        d["ctrl_disk"] = cf.get("disk", "?"); d["ctrl_total_vcpu"] = cf.get("total_vcpu", "?")

    return d


def _peak_from_traj():
    """Best-effort: peak pod count from the newest guard trajectory log."""
    rd = os.path.join(REPO, "drivers", "results")
    try:
        trajs = sorted((f for f in os.listdir(rd) if f.endswith(".traj.log")), reverse=True)
        if not trajs:
            return "?"
        txt = open(os.path.join(rd, trajs[0])).read()
        peaks = re.findall(r"peak=(\d+)", txt)
        return max(int(p) for p in peaks) if peaks else "?"
    except Exception:
        return "?"


# canonical phase order for the metrics table; any extra phases in the JSON are
# appended after these so the table is always COMPLETE (never drops a phase).
_PHASE_ORDER = ["preflight", "plan", "prepull", "create_warmpool", "wait_pool_ready",
                "prefetch", "claim", "process", "reset", "quarantine", "rotate",
                "release", "teardown"]


def _all_phases(variants):
    seen = []
    for v in variants:
        for p in (v.get("phases") or {}):
            if p not in seen:
                seen.append(p)
    ordered = [p for p in _PHASE_ORDER if p in seen]
    return ordered + [p for p in seen if p not in _PHASE_ORDER]


def variants_summary_table(variants):
    """Headline sweep table: one row per variant, every top-level metric."""
    head = ("| variant | strategy | recycle | wall (s) | report_total (s) | s/task | "
            "throughput (t/s) | claims | tasks ok | tasks err | peak warm |\n"
            "|---|---|:--:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    rows = []
    for v in variants:
        rows.append("| `{label}` | {strat} | {rec} | {wall} | {rtot} | {spt} | {thr} | "
                    "{claims} | {ok} | {err} | {peak} |".format(
                        label=v.get("label", "?"), strat=v.get("strategy", "?"),
                        rec="yes" if v.get("recycle") else "no",
                        wall=v.get("wall_s", "?"), rtot=v.get("report_total_s", "—"),
                        spt=v.get("s_per_task", "?"),
                        thr=v.get("throughput_tasks_per_s", "—"),
                        claims=v.get("claims") if v.get("claims") is not None else "—",
                        ok=v.get("tasks_ok", "?"), err=v.get("tasks_err", "?"),
                        peak=v.get("warm_replicas_peak") if v.get("warm_replicas_peak") is not None else "—"))
    return head + "\n".join(rows)


def variants_phase_table(variants):
    """COMPLETE per-variant phase metrics (long format), from the SDK RunReport —
    one row per (variant, phase): n, avg, total, max seconds. Every phase any
    variant recorded is included."""
    head = ("| variant | phase | n | avg (s) | total (s) | max (s) |\n"
            "|---|---|---:|---:|---:|---:|\n")
    phases = _all_phases(variants)
    rows = []
    for v in variants:
        ph = v.get("phases") or {}
        for p in phases:
            s = ph.get(p)
            if not s:
                continue
            rows.append(f"| `{v.get('label','?')}` | {p} | {s.get('n','—')} | "
                        f"{s.get('avg','—')} | {s.get('total','—')} | {s.get('max','—')} |")
    if not rows:
        return "_No phase metrics recorded._"
    return head + "\n".join(rows)


def run_report_blocks(variants):
    """Per-variant **Full RunReport** in the SDK's native `RunReport.summary()`
    layout — `<phase>  <total>s  (n=<count>, max=<max>s)`, then TOTAL wall + the
    claims/tasks/warm-peak line. One fenced block per variant."""
    blocks = []
    for v in variants:
        ph = v.get("phases") or {}
        names = ([p for p in _PHASE_ORDER if p in ph]
                 + [p for p in ph if p not in _PHASE_ORDER])
        lines = [f"Full RunReport — {v.get('label','?')} "
                 f"(strategy={v.get('strategy','?')}, recycle={bool(v.get('recycle'))})"]
        for name in names:
            s = ph[name]
            total = float(s.get("total") or 0.0)
            cnt = s.get("n") or 0
            mx = float(s.get("max") or 0.0)
            lines.append(f"  {name:<18} {total:8.2f}s  (n={cnt}, max={mx:.2f}s)")
        rtot = float(v.get("report_total_s") or v.get("wall_s") or 0.0)
        lines.append(f"  {'TOTAL wall':<18} {rtot:8.2f}s")
        claims = v.get("claims")
        peak = v.get("warm_replicas_peak")
        lines.append(f"  claims={claims if claims is not None else '—'}  "
                     f"tasks={v.get('tasks_ok','?')}ok/{v.get('tasks_err','?')}err  "
                     f"warm peak={peak if peak is not None else '—'}")
        blocks.append("```\n" + "\n".join(lines) + "\n```")
    return "\n\n".join(blocks)


def params_table(prm, extra):
    """COMPLETE two-column dump of every parameter the driver ran with (the JSON
    ``params`` block) plus report-time cluster/env facts — so the report captures
    the exact run config, nothing curated away."""
    rows = []
    for k, v in prm.items():
        rows.append(f"| `{k}` | {v} |")
    for k, v in extra.items():
        rows.append(f"| `{k}` | {v} |")
    return "| parameter | value |\n|---|---|\n" + "\n".join(rows)


def run_command(prm, driver):
    """Reconstruct the exact driver invocation from the recorded params."""
    sel = prm.get("node_selector") or {}
    ns_arg = ",".join(f"{k}={v}" for k, v in sel.items()) if isinstance(sel, dict) else sel
    parts = [f"python drivers/{driver}",
             f"--context {prm.get('context', '?')}",
             f"--namespace {prm.get('namespace', '?')}"]
    if ns_arg:
        parts.append(f"--node-selector {ns_arg}")
    parts += [
        f"--images-file {prm.get('images_file', '?')}",
        f"--problems {prm.get('images', '?')}",
        f"--rollouts {prm.get('rollouts', '?')}",
        f"--concurrency {prm.get('concurrency', '?')}",
        f"--claim-concurrency {prm.get('claim_concurrency', '?')}",
        f"--cpu {prm.get('cpu', '?')}",
        f"--memory {prm.get('memory', '?')}",
        f"--runtime-class {prm.get('runtime_class', '?')}",
        f"--testbed {prm.get('testbed', '?')}",
        f"--probe '{prm.get('probe', '?')}'",
        f"--strategies {prm.get('strategies', '?')}",
        f"--max-reuses {prm.get('max_reuses', '?')}",
    ]
    if prm.get("recycle"):
        parts.append("--recycle")
    parts.append(f"--out {prm.get('out', '?')}")
    return " \\\n  ".join(parts)


def main():
    p = argparse.ArgumentParser(description="Render comprehensive benchmark report + RESULTS row")
    p.add_argument("results_json")
    p.add_argument("--user", default=None)
    p.add_argument("--tier", default="?")
    p.add_argument("--daemon", default="stock")
    p.add_argument("--start", default="warm")
    p.add_argument("--images-src", default=None)
    p.add_argument("--driver", default="run_basic_swebench_3000.py")
    # auto-collected; flags override if given
    p.add_argument("--gke", default=None)
    p.add_argument("--sbd", default=None)
    p.add_argument("--imgstream", default=None)
    p.add_argument("--ctrl", default=None, help="sandbox/warmpool workers, e.g. 1000/10")
    p.add_argument("--maxpods", default=None)
    p.add_argument("--peak", default=None)
    p.add_argument("--note", default="")
    p.add_argument("--no-cluster-info", action="store_true", help="skip the live cluster query")
    p.add_argument("--now", default=None)
    p.add_argument("--append", action="store_true")
    a = p.parse_args()

    with open(a.results_json) as fh:
        dd = json.load(fh)
    prm = dd.get("params", {})
    prm["driver"] = a.driver          # the runnable driver script (JSON records the engine module)
    ctx = prm.get("context", "")
    ns = prm.get("namespace", "default")
    pool = (prm.get("node_selector") or {}).get("cloud.google.com/gke-nodepool", "?")

    ci = collections.defaultdict(lambda: "?") if a.no_cluster_info else collect_cluster(ctx, pool, ns)

    user = a.user or default_user()
    now = a.now or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"{slug(user)}_{stamp}"

    # arg-override-else-auto
    gke = a.gke or ci["k8s"]
    sbd = a.sbd or ci["sbd"]
    imgstream = a.imgstream or ci["imgstream"]
    ctrl = a.ctrl or ci["ctrl_workers"]
    maxpods = a.maxpods or ci["node_pods"]
    peak = a.peak or _peak_from_traj()
    machine = ci["machine"]
    disk = ci["disk"]
    images_src = a.images_src or "us-docker.pkg.dev/<project>/swebench-mirror/swebench-verified"

    variants = dd.get("variants") or []
    if not variants:
        raise SystemExit("results JSON has no 'variants' — re-run with the current "
                         "bench_core (sweep model: fleet.run(strategy, recycle)).")

    errs = []
    for v in variants:
        for e in v.get("errors_sample", []):
            errs.append(f"[{v.get('label')}] {e}")
    errors_md = "None reported." if not errs else "\n".join(f"- `{e}`" for e in errs[:16])

    summary_table = variants_summary_table(variants)
    phase_table = variants_phase_table(variants)          # COMPLETE metrics (all phases)
    runreport_blocks = run_report_blocks(variants)        # native RunReport.summary() layout
    # best (lowest s/task) variant for the headline one-liner
    best = min(variants, key=lambda v: (v.get("s_per_task") is None, v.get("s_per_task") or 9e9))

    total_tasks = prm.get("total_tasks", "?")   # result-row fallback per variant

    # COMPLETE parameter dump: every knob the driver ran with (the JSON params
    # block) + the report-time cluster/env facts. Nothing curated away.
    env_extra = {
        "tier": a.tier, "gke_version": gke, "cluster": ci["cluster_name"],
        "sandbox_pool": pool, "sandbox_node_machine": ci["machine"], "maxPods": maxpods,
        "controller_node_machine": ci["ctrl_machine"], "controller_pool": ci["ctrl_pool"],
        "controller_image": ci["ctrl_image"], "ctrl_workers(sbx/wp)": ctrl,
        "sbd": sbd, "image_streaming": imgstream, "daemon": a.daemon,
        "images_src": images_src, "peak_pods": peak,
    }
    params_tbl = params_table(prm, env_extra)
    run_cmd = run_command(prm, a.driver)

    # one RESULTS.md row per variant (each is a comparable line), numbered continuing
    # from the current max '#' in RESULTS.md (keeps the perf-at-a-glance chart's #N refs valid)
    start_num = _next_row_num(RESULTS)
    result_rows = []
    for i, v in enumerate(variants):
        cells = [
            start_num + i,
            f"{run_id}-{slug(v.get('label', 'v'))}", user, a.tier, gke, a.daemon, a.start,
            v.get("strategy", "?"), "yes" if v.get("recycle") else "no",
            v.get("total_tasks", total_tasks), prm.get("images", "?"), prm.get("rollouts", "?"),
            prm.get("concurrency", "?"), maxpods, prm.get("cpu", "?"), prm.get("memory", "?"),
            sbd, imgstream, ctrl,
            v.get("wall_s", "?"), v.get("s_per_task", "?"),
            v.get("claims", "?") if v.get("claims") is not None else "—",
            f"{v.get('tasks_ok', '?')}/{v.get('tasks_err', '?')}",
            a.note.replace("|", "/"),
        ]
        result_rows.append("| " + " | ".join(str(c) for c in cells) + " |")
    results_rows = "\n".join(result_rows)

    repl = {
        "USERNAME": user, "TIMESTAMP": now, "TIER": a.tier, "CLUSTER": ci["cluster_name"],
        "POOL": pool, "START": a.start, "NOTE": a.note or "—", "DRIVER": a.driver,
        # headline
        "BEST_LABEL": best.get("label", "?"), "BEST_S_PER_TASK": best.get("s_per_task", "?"),
        "BEST_WALL": best.get("wall_s", "?"), "PEAK_PODS": peak,
        "SUMMARY_TABLE": summary_table, "METRICS_TABLE": phase_table,
        "RUNREPORT_BLOCKS": runreport_blocks,
        "PARAMS_TABLE": params_tbl, "RUN_COMMAND": run_cmd,
        # per-pod (cluster resources line) + methodology probe note
        "CPU": prm.get("cpu", "?"), "MEM": prm.get("memory", "?"),
        "PROBE": prm.get("probe", "?"),
        # cluster
        "PROJECT": ci["project"], "REGION": ci["region"], "K8S": gke, "CTRL_IMAGE": ci["ctrl_image"],
        "POOL_NODES": ci["pool_nodes"], "MACHINE": machine, "ZONES": ci["zones"], "DISK": disk,
        "NODE_VCPU": ci["node_vcpu"], "NODE_MEM": ci["node_mem"], "NODE_EPH": ci["node_eph"],
        "NODE_PODS": maxpods, "TAINT": ci["taint"], "TOTAL_VCPU": ci["total_vcpu"],
        "TOTAL_PODS": ci["total_pods"], "IMGSTREAM": imgstream, "SBD": sbd,
        "IMAGES_SRC": images_src,
        "CTRL_FLAGS": ci["ctrl_flags"], "CTRL": ctrl,
        "CTRL_MACHINE": ci["ctrl_machine"], "CTRL_POOL": ci["ctrl_pool"],
        "CTRL_NODES": ci["ctrl_nodes"], "CTRL_ZONES": ci["ctrl_zones"],
        "CTRL_DISK": ci["ctrl_disk"], "CTRL_VCPU": ci["ctrl_vcpu"],
        "CTRL_MEM": ci["ctrl_mem"], "CTRL_EPH": ci["ctrl_eph"],
        "CTRL_NODE_PODS": ci["ctrl_node_pods"], "CTRL_TAINT": ci["ctrl_taint"],
        "CTRL_TOTAL_VCPU": ci["ctrl_total_vcpu"],
        "RESULTS_ROWS": results_rows, "ERRORS": errors_md,
    }
    with open(TEMPLATE) as fh:
        report = fh.read()
    for k, v in repl.items():
        report = report.replace("{{" + k + "}}", str(v))

    os.makedirs(REPORTS, exist_ok=True)
    out_path = os.path.join(REPORTS, f"{run_id}.md")
    with open(out_path, "w") as fh:
        fh.write(report)
    if a.append and os.path.exists(RESULTS):
        with open(RESULTS, "a") as fh:
            fh.write(results_rows + "\n")

    print(f"report: {os.path.relpath(out_path, REPO)}")
    print("\nRESULTS.md row(s):\n" + results_rows)


if __name__ == "__main__":
    main()
