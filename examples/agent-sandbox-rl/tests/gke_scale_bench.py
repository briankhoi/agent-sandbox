# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Benchmarks `FleetConfig.claim_mode` at GKE scale: per-claim against batch.

Not a unit test (it needs a live cluster); the filename keeps pytest from
collecting it. Pure helpers are unit-tested in ``test_gke_scale_bench.py``.

The kind driver (``claim_mode_bench.py``) measured one step of 160 sandboxes on a
fresh fleet. This one reuses its arms and request counting and adds what a kind
node can't show:
  sizes        160, 640 and 1280 sandboxes per step (RL_BENCH_SIZES), 4 tasks per
               image, so a size of N uses the first N/4 of one shared set of pools
  steps        each run is one fleet that runs RL_BENCH_STEPS steps. Step 1 pays the
               fleet's one-time costs (client build, batch open); steps 2 and later
               are the steady state a long training run sees, and lead the report
  cold         the same sizes with every pool at 0 replicas, so each claim creates
               its sandbox, and batch's create_rps pacing applies
  API server   the API server's own counters (/metrics) before and after each step:
               requests by verb and resource, mean server-side latency, priority
               and fairness queue wait and rejections, so the load each arm puts on
               the API server shows up as well as the requests the driver sends.
               A zonal cluster has one API server, so a delta is meaningful; the
               delta is marked invalid when its claim creates don't match the
               driver's own count
  connections  urllib3's "Connection pool is full" discards, per step

Subcommands:
  images   print the image references the pools use, one per line
  setup    create the namespace, templates and warm pools (all pools, 0 replicas)
  suite    run every planned run that has no result file yet, in plan order
  status   regenerate STATUS.md from the result files
  report   write summary.md from the result files

Each run writes ``<results>/<label>.json`` once it finishes and never overwrites
one, so a stopped suite resumes where it left off.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import queue
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claim_mode_bench as kb  # noqa: E402
from claim_mode_bench import (COUNTER, NAMESPACE, RUN_ID, THROTTLE, Variant,  # noqa: E402
                              burst_out, cpu_seconds, interleave, median_of, ratio,
                              run_async_step)

PKG = kb.PKG
RESULTS = os.environ.get("RL_BENCH_RESULTS", os.path.join(PKG, "bench-results", "gke"))
IMAGE_REPO = os.environ.get("RL_BENCH_IMAGE_REPO", "bench.local/rl-task")
SMOKE = os.environ.get("RL_BENCH_SMOKE") == "1"
PER_IMAGE = 4


def _sizes(name: str, default: str) -> list[int]:
    raw = os.environ.get(name, default).strip()
    sizes = [int(s) for s in raw.split(",") if s.strip()] if raw else []
    for s in sizes:
        if s <= 0 or s % PER_IMAGE:
            raise ValueError(f"{name}: {s} is not a positive multiple of {PER_IMAGE}")
    return sizes


SIZES = [8] if SMOKE else _sizes("RL_BENCH_SIZES", "160,640,1280")
COLD_SIZES = [8] if SMOKE else _sizes("RL_BENCH_COLD_SIZES", "160,640")
ASYNC_SIZES = [8] if SMOKE else _sizes("RL_BENCH_ASYNC_SIZES", "640")
STEPS = 2 if SMOKE else int(os.environ.get("RL_BENCH_STEPS", "3"))
RUNS = 1 if SMOKE else int(os.environ.get("RL_BENCH_RUNS", "3"))
POOLS = max(SIZES + COLD_SIZES + ASYNC_SIZES) // PER_IMAGE
STEP_DEADLINE_S = 900


def image(i: int) -> str:
    return f"{IMAGE_REPO}:g{i:03d}"


def all_images() -> list[str]:
    return [image(i) for i in range(POOLS)]


@dataclass(frozen=True)
class Shape:
    """``size`` tasks over the first ``size // PER_IMAGE`` pools, warm (each of
    those pools at PER_IMAGE replicas) or cold (every pool at 0)."""

    size: int
    warm: bool = True
    per_image: int = PER_IMAGE

    @property
    def name(self) -> str:
        return f"{'w' if self.warm else 'c'}{self.size}"

    @property
    def tasks(self) -> int:
        return self.size

    @property
    def images(self) -> int:
        return self.size // self.per_image

    def image_list(self) -> list[str]:
        return [image(i) for i in range(self.images)]


# Every warm claim is covered by its pool, so the SDK sends it unpaced and only
# max_in_flight bounds the creates; create_rps applies to cold claims.
# The SDK's own caps lifted, so the lead pair has no limit on either arm. The SDK
# refuses a max_in_flight its connection pool can't hold (pool >= max_in_flight +
# BATCH_POOL_HEADROOM), and the RL library's pool is 1000, so 998 is the most it
# allows; per-claim meets the same pool as extra one-off connections past 1000.
UNBOUNDED_MIF = 998
UNBOUNDED_RPS = 1e6
WARM_VARIANTS = [
    Variant("per-claim", "per-claim"),
    Variant("batch-unbounded", "batch", create_rps=UNBOUNDED_RPS, max_in_flight=UNBOUNDED_MIF),
    # What users get: the SDK's 20 in-flight creates (and 50/s, which warm claims skip).
    Variant("batch-default", "batch"),
    # The same driver semaphore on both arms, the size the kind runs used.
    Variant("per-claim-c50", "per-claim", acquire_concurrency=50),
    Variant("batch-c50", "batch", acquire_concurrency=50),
    # One claim_batch per step with every group sized to its tasks (the group
    # size > 0 path a wave caller would use), envs fed from events(). Compared with
    # the acquire burst at the same settings, it measures the wave mode's gain.
    Variant("fill-unbounded", "fill", create_rps=UNBOUNDED_RPS, max_in_flight=UNBOUNDED_MIF),
    Variant("fill-default", "fill"),
]
ASYNC_VARIANTS = [
    Variant("async-per-claim", "per-claim", loop="async"),
    Variant("async-batch-unbounded", "batch", create_rps=UNBOUNDED_RPS,
            max_in_flight=UNBOUNDED_MIF, loop="async"),
]
COLD_VARIANTS = [
    Variant("per-claim", "per-claim"),
    Variant("batch-unbounded", "batch", create_rps=UNBOUNDED_RPS, max_in_flight=UNBOUNDED_MIF),
    # The SDK default paces cold creates at 50/s; per-claim sends them unpaced.
    Variant("batch-default", "batch"),
]
# Fixed before the run (rl_design_decision.md, the cohort rule): the wave path is a
# clear gain when the fill's last start is more than 20% below the acquire burst's
# at 640 sandboxes or more.
WAVE_GAIN = 0.2
WAVE_MIN_SIZE = 640


@dataclass(frozen=True)
class PlannedRun:
    label: str
    variant: Variant
    run: int
    shape: Shape
    steps: int


def run_plan() -> list[PlannedRun]:
    """Smallest size first, warm before cold, runs of one block interleaved with
    the order rotated each round (``interleave``). The async arms run one step,
    since the kind async run found nothing that steps would change."""
    plan = []
    for size in SIZES:
        shape = Shape(size)
        for v, k in interleave(WARM_VARIANTS, RUNS):
            plan.append(PlannedRun(f"{shape.name}-{v.name}-run{k}", v, k, shape, STEPS))
        if size in ASYNC_SIZES:
            for v, k in interleave(ASYNC_VARIANTS, RUNS):
                plan.append(PlannedRun(f"{shape.name}-{v.name}-run{k}", v, k, shape, 1))
    for size in COLD_SIZES:
        shape = Shape(size, warm=False)
        for v, k in interleave(COLD_VARIANTS, RUNS):
            plan.append(PlannedRun(f"{shape.name}-{v.name}-run{k}", v, k, shape, STEPS))
    return plan


# --------------------------------------------------------------------------- #
# API server metrics (pure parts unit-tested)
# --------------------------------------------------------------------------- #
APISERVER_RESOURCES = ("sandboxclaims", "sandboxes", "sandboxwarmpools",
                       "sandboxtemplates", "leases", "pods", "services")
# The resources a claim's own requests touch, from any client (the controller's
# included), so the count doesn't grow with the step's length the way every
# request in the cluster does.
CLAIM_PATH_RESOURCES = ("sandboxclaims", "sandboxes", "leases")
_KEPT = ("apiserver_request_total", "apiserver_request_duration_seconds_sum",
         "apiserver_request_duration_seconds_count",
         "apiserver_flowcontrol_rejected_requests_total",
         "apiserver_flowcontrol_request_wait_duration_seconds_sum",
         "apiserver_flowcontrol_request_wait_duration_seconds_count")


def _labels(raw: str) -> dict[str, str]:
    out = {}
    for part in raw.split('",'):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().lstrip(",")] = v.strip().strip('"')
    return out


def parse_apiserver_metrics(text: str) -> dict[str, float]:
    """Sums the kept series into flat keys: ``requests VERB resource code``,
    ``latency_sum VERB resource``, ``latency_count VERB resource``, ``apf_rejected``,
    ``apf_wait_sum`` and ``apf_wait_count`` (requests that executed)."""
    out: dict[str, float] = {}

    def add(key, v):
        out[key] = out.get(key, 0.0) + v

    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        name, _, rest = line.partition("{")
        if name not in _KEPT:
            continue
        labels_raw, _, value = rest.rpartition("}")
        try:
            v = float(value.strip().split()[0])
        except (ValueError, IndexError):
            continue
        lb = _labels(labels_raw)
        res = lb.get("resource", "")
        verb = lb.get("verb", "")
        if name == "apiserver_request_total":
            if res in APISERVER_RESOURCES:
                add(f"requests {verb} {res} {lb.get('code', '')}", v)
            add("requests_all", v)
            if lb.get("code") == "429":
                add("http_429_all", v)
        elif name == "apiserver_request_duration_seconds_sum" and res in APISERVER_RESOURCES:
            add(f"latency_sum {verb} {res}", v)
        elif name == "apiserver_request_duration_seconds_count" and res in APISERVER_RESOURCES:
            add(f"latency_count {verb} {res}", v)
        elif name == "apiserver_flowcontrol_rejected_requests_total":
            add("apf_rejected", v)
        elif name.startswith("apiserver_flowcontrol_request_wait") and lb.get("execute") == "true":
            add("apf_wait_sum" if name.endswith("_sum") else "apf_wait_count", v)
    return out


def apiserver_delta(before: dict[str, float] | None, after: dict[str, float] | None,
                    driver_creates: int) -> dict:
    """The step's share of the counters. ``valid`` is false when the API server's
    claim creates don't equal the driver's, which happens when the two scrapes hit
    different API server replicas or the counters reset (a control plane restart)."""
    if before is None or after is None:
        return {"valid": False, "reason": "scrape failed"}
    d = {k: after.get(k, 0.0) - before.get(k, 0.0) for k in set(before) | set(after)}
    if any(v < 0 for v in d.values()):
        return {"valid": False, "reason": "a counter went down (restart or another replica)"}
    creates = sum(v for k, v in d.items() if k.startswith("requests POST sandboxclaims "))
    requests = {k[len("requests "):]: int(v) for k, v in d.items()
                if k.startswith("requests ") and v}
    latency_ms = {}
    for k, v in d.items():
        if k.startswith("latency_count ") and v:
            key = k[len("latency_count "):]
            latency_ms[key] = round(1000 * d.get(f"latency_sum {key}", 0.0) / v, 2)
    claim_path = sum(v for k, v in d.items() if k.startswith("requests ")
                     and k.split(" ")[2] in CLAIM_PATH_RESOURCES)
    out = {"valid": int(creates) == driver_creates, "claim_creates": int(creates),
           "claim_path_requests": int(claim_path),
           "driver_creates": driver_creates, "requests": requests,
           "requests_all": int(d.get("requests_all", 0)),
           "http_429_all": int(d.get("http_429_all", 0)),
           "mean_latency_ms": latency_ms,
           "apf_rejected": int(d.get("apf_rejected", 0)),
           "apf_wait_s": round(d.get("apf_wait_sum", 0.0), 3),
           "apf_requests": int(d.get("apf_wait_count", 0))}
    if not out["valid"]:
        out["reason"] = "API server claim creates differ from the driver's"
    return out


def scrape_apiserver(api) -> dict[str, float] | None:
    """One /metrics scrape, counted as a driver request so neither arm pays it."""
    try:
        method, url, headers, _, _ = api.param_serialize(
            "GET", "/metrics", header_params={"Accept": "text/plain"},
            auth_settings=["BearerToken"])
        with COUNTER.driver():
            resp = api.call_api(method, url, header_params=headers, _request_timeout=30)
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"HTTP {resp.status}")
        return parse_apiserver_metrics(resp.read().decode())
    except Exception as e:  # noqa: BLE001 — the step still counts without it
        print(f"apiserver metrics scrape failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


class PoolFullLog(logging.Handler):
    """Counts urllib3's "Connection pool is full, discarding connection": a request
    that ran while every pooled connection was busy opened its own connection
    (a new TCP and TLS handshake) and dropped it afterwards."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record):
        if "Connection pool is full" in record.getMessage():
            self.count += 1

    def install(self) -> None:
        logging.getLogger("urllib3.connectionpool").addHandler(self)


POOL_FULL = PoolFullLog()


# --------------------------------------------------------------------------- #
# Aggregation and the verdict (pure; unit-tested)
# --------------------------------------------------------------------------- #
STEP_KEYS = ("last_start_s", "first_start_s", "median_start_s", "total_requests",
             "watches", "driver_cpu_s", "pool_full_discards", "creates_per_s",
             "create_window_s")


def steady_of(steps: list[dict]) -> dict:
    """Per-run summary of steps 2 and later (step 1 alone when there is only one):
    the median of each key over those steps, plus the Ready distribution's
    median p50, p99 and last."""
    use = steps[1:] if len(steps) > 1 else steps
    out = {"steps_used": len(use)}
    for k in STEP_KEYS:
        out[k] = median_of(use, k)
    for stat in ("p50", "p99", "last"):
        vals = [s["acquired_s"][stat] for s in use if (s.get("acquired_s") or {}).get(stat)
                is not None]
        out[f"ready_{stat}"] = statistics.median(vals) if vals else None
    lat = [s["apiserver"]["mean_latency_ms"].get("POST sandboxclaims") for s in use
           if (s.get("apiserver") or {}).get("valid")]
    lat = [v for v in lat if v is not None]
    out["apiserver_create_ms"] = statistics.median(lat) if lat else None
    for k in ("apf_wait_s", "requests_all", "claim_path_requests", "apf_rejected",
              "http_429_all"):
        vals = [s["apiserver"][k] for s in use if (s.get("apiserver") or {}).get("valid")]
        out[f"apiserver_{k}"] = statistics.median(vals) if vals else None
    return out


def summarize(results: list[dict]) -> dict:
    """Rows keyed by ``<shape>/<variant>``, each the median over runs of the
    per-run steady values, with the min and max of the headline over runs."""
    by: dict[str, list[dict]] = {}
    for r in results:
        if r.get("exception"):
            continue
        by.setdefault(f"{r['shape']}/{r['variant']}", []).append(r)
    rows = {}
    for key, rs in by.items():
        steady = [r["steady"] for r in rs]
        first = [r["steps"][0] for r in rs if r.get("steps")]
        row = {"runs": len(rs), "errors": sum(r.get("errors", 0) for r in rs)}
        for k in steady[0]:
            row[k] = median_of(steady, k)
        ready_last = [s["ready_last"] for s in steady if s.get("ready_last") is not None]
        row["ready_last_min"] = min(ready_last) if ready_last else None
        row["ready_last_max"] = max(ready_last) if ready_last else None
        row["step1_ready_last"] = statistics.median(
            [f["acquired_s"]["last"] for f in first if f.get("acquired_s")] or [0.0]) \
            if first else None
        row["step1_last_start"] = median_of(first, "last_start_s")
        row["n"] = rs[0].get("n")
        rows[key] = row
    return rows


# A ratio is batch over per-claim, so under 1 means batch is faster. Fixed before the
# run: a win needs the batch median at most WIN of per-claim's and the two ranges
# over runs not to overlap; a loss is the mirror image.
WIN = 0.9


def classify(batch: dict | None, base: dict | None, key: str = "ready_last") -> str | None:
    if not batch or not base or batch.get("errors") or base.get("errors"):
        return None
    b, p = batch.get(key), base.get(key)
    if b is None or not p:
        return None
    blo, bhi = batch.get(f"{key}_min", b), batch.get(f"{key}_max", b)
    plo, phi = base.get(f"{key}_min", p), base.get(f"{key}_max", p)
    if b <= WIN * p and bhi < plo:
        return "batch wins"
    if p <= WIN * b and phi < blo:
        return "batch loses"
    return "inside noise"


def verdict(rows: dict) -> dict:
    out: dict = {}
    shapes = sorted({k.split("/")[0] for k in rows}, key=lambda s: (s[0] != "w", int(s[1:])))
    for shape in shapes:
        def row(v, shape=shape):
            return rows.get(f"{shape}/{v}")

        pairs = ([("no_limit", "batch-unbounded", "per-claim"),
                  ("defaults", "batch-default", "per-claim"),
                  ("c50", "batch-c50", "per-claim-c50"),
                  ("async_no_limit", "async-batch-unbounded", "async-per-claim"),
                  ("wave_unbounded", "fill-unbounded", "batch-unbounded"),
                  ("wave_default", "fill-default", "batch-default")]
                 if shape[0] == "w" else
                 [("no_limit", "batch-unbounded", "per-claim"),
                  ("defaults", "batch-default", "per-claim")])
        s = {}
        for name, b, p in pairs:
            rb, rp = row(b), row(p)
            if rb is None or rp is None:
                continue
            s[name] = {"ratio": ratio(rb.get("ready_last"), rp.get("ready_last")),
                       "call": classify(rb, rp),
                       "last_start_ratio": ratio(rb.get("last_start_s"),
                                                 rp.get("last_start_s")),
                       "step1_ratio": ratio(rb.get("step1_ready_last"),
                                            rp.get("step1_ready_last"))}
        for name in ("wave_unbounded", "wave_default"):
            r = (s.get(name) or {}).get("last_start_ratio")
            if r is not None and int(shape[1:]) >= WAVE_MIN_SIZE:
                s[name]["gain_over_20pct"] = r < 1 - WAVE_GAIN
        b, p = row("batch-unbounded"), row("per-claim")
        if b and p:
            s["requests_per_env"] = {
                "batch": ratio(b.get("total_requests"), b.get("n")),
                "per_claim": ratio(p.get("total_requests"), p.get("n"))}
            s["watches"] = {"batch": b.get("watches"), "per_claim": p.get("watches")}
            s["apiserver_create_ms"] = {"batch": b.get("apiserver_create_ms"),
                                        "per_claim": p.get("apiserver_create_ms")}
            s["claim_path_requests"] = {"batch": b.get("apiserver_claim_path_requests"),
                                        "per_claim": p.get("apiserver_claim_path_requests")}
            s["apf_wait_s"] = {"batch": b.get("apiserver_apf_wait_s"),
                               "per_claim": p.get("apiserver_apf_wait_s")}
            s["pool_full_discards"] = {"batch": b.get("pool_full_discards"),
                                       "per_claim": p.get("pool_full_discards")}
            s["tail_p99_ratio"] = ratio(b.get("ready_p99"), p.get("ready_p99"))
        out[shape] = s
    warm = [(int(k[1:]), v["no_limit"]["ratio"]) for k, v in out.items()
            if k[0] == "w" and (v.get("no_limit") or {}).get("ratio") is not None]
    warm.sort()
    out["warm_ratio_by_size"] = warm
    # Context only: the two ends, with no noise test.
    out["win_grows_with_size"] = (warm[-1][1] < warm[0][1]) if len(warm) >= 2 else None
    return out


# --------------------------------------------------------------------------- #
# Cluster plumbing
# --------------------------------------------------------------------------- #
# Where sandbox pods run: a node pool (so they stay off the driver and system
# nodes) and a RuntimeClass such as gvisor. Templates are created by setup, so set
# these before setup and keep them for every run.
NODE_POOL = os.environ.get("RL_BENCH_NODE_POOL") or None
RUNTIME_CLASS = os.environ.get("RL_BENCH_RUNTIME_CLASS") or None


def cluster_config(args):
    from agent_sandbox_rl import ClusterConfig
    where = dict(name="gke", namespace=args.namespace, runtime_class=RUNTIME_CLASS,
                 node_selector={"cloud.google.com/gke-nodepool": NODE_POOL} if NODE_POOL
                 else None)
    if args.in_cluster:
        return ClusterConfig(in_cluster=True, **where)
    return ClusterConfig(context=args.context, **where)


def fleet_config(args, variant: Variant | None, shape: Shape):
    """The kind driver's config with this cluster and a concurrency budget of the
    step's size, so neither the fleet's plan nor its async pool caps a step."""
    return kb.fleet_config(args, variant, clusters=[cluster_config(args)],
                           max_concurrent=max(shape.tasks, 1))


def k8s(args):
    from agent_sandbox_rl.cluster import build_api_client
    from kubernetes import client
    api = build_api_client(cluster_config(args))
    return client.CoreV1Api(api), client.CustomObjectsApi(api), api


def pool_names(args) -> list[str]:
    cfg = kb.fleet_config(args, None, clusters=[cluster_config(args)])
    return [cfg.pool_name(img) for img in all_images()]


def pool_states(custom, args) -> dict[str, tuple[int, int]]:
    """(spec replicas, readyReplicas) of every warm pool in the namespace, from one
    list, since a GET per pool is hundreds of requests per poll at 1280."""
    from agent_sandbox_rl import constants
    items = custom.list_namespaced_custom_object(
        constants.GROUP, constants.VERSION, args.namespace,
        constants.WARMPOOLS_PLURAL).get("items", [])
    return {o["metadata"]["name"]: (int((o.get("spec") or {}).get("replicas") or 0),
                                    int((o.get("status") or {}).get("readyReplicas") or 0))
            for o in items}


def scale_for(args, custom, shape: Shape) -> None:
    """Pools of a warm shape to PER_IMAGE replicas, every other pool to 0."""
    from agent_sandbox_rl import constants
    names = pool_names(args)
    for i, p in enumerate(names):
        want = shape.per_image if shape.warm and i < shape.images else 0
        custom.patch_namespaced_custom_object(
            constants.GROUP, constants.VERSION, args.namespace,
            constants.WARMPOOLS_PLURAL, p, {"spec": {"replicas": want}})


def wait_state(args, shape: Shape, timeout: float = 1800) -> dict:
    """Blocks until the shape's pools are at their replicas and Ready, every other
    pool is empty, no claim of this run exists and no pod is terminating, then
    settles. Cold shapes wait for an empty namespace."""
    core, custom, _ = k8s(args)
    names = pool_names(args)
    t0 = time.monotonic()
    while True:
        states = pool_states(custom, args)
        claims = kb.run_claims(custom, args)
        total, terminating = kb.pod_counts(core, args)
        ok = True
        for i, p in enumerate(names):
            want = shape.per_image if shape.warm and i < shape.images else 0
            spec, ready = states.get(p, (-1, 0))
            if spec != want or (want and ready < want):
                ok = False
                break
        if not shape.warm and total:
            ok = False
        if ok and claims == 0 and terminating == 0:
            break
        if time.monotonic() - t0 > timeout:
            short = {p: s for p, s in states.items() if s[1] < s[0]}
            raise RuntimeError(f"state {shape.name} not reached in {timeout}s: claims={claims} "
                               f"terminating={terminating} pods={total} "
                               f"pools_short={len(short)}")
        time.sleep(5)
    time.sleep(5)
    return {"warm_wait_s": time.monotonic() - t0, "pods_in_namespace": total}


def arm_fleet(args, variant: Variant, shape: Shape):
    from agent_sandbox_rl import SandboxFleet
    f = SandboxFleet(fleet_config(args, variant, shape))
    f.load_tasks([img for img in shape.image_list() for _ in range(shape.per_image)])
    f.plan()
    return f


def provenance() -> dict:
    import kubernetes
    out = kb.provenance()
    out.update(kubernetes=kubernetes.__version__, sizes=SIZES, cold_sizes=COLD_SIZES,
               async_sizes=ASYNC_SIZES, steps=STEPS, runs=RUNS, image_repo=IMAGE_REPO,
               node_pool=NODE_POOL, runtime_class=RUNTIME_CLASS, cpus=os.cpu_count())
    return out


# --------------------------------------------------------------------------- #
# One step and one run
# --------------------------------------------------------------------------- #
def run_step(args, fleet, cluster, api, variant: Variant, shape: Shape) -> dict:
    """Every env acquires at once (or through the driver semaphore), execs once,
    waits for the step to end, then releases; the fill arm takes its members from
    one claim_batch instead. The step's clock stops when every env has started."""
    n = shape.tasks
    starts: dict[int, float] = {}
    acquired: dict[int, float] = {}
    errors: list[str] = []
    lock = threading.Lock()
    go = threading.Event()
    step_end = threading.Event()
    sem = (threading.Semaphore(variant.acquire_concurrency)
           if variant.acquire_concurrency else None)
    fill_queues: dict[str, queue.Queue] = {}
    fill: dict = {}
    t0 = 0.0

    def env(i: int, task):
        try:
            go.wait()
            if variant.arm == "fill":
                pool = fleet.plan_.for_image(task.image).pool
                member = fill_queues[pool].get(timeout=STEP_DEADLINE_S)
                if member is None:
                    raise RuntimeError("fill ended before this env got a member")
                with lock:
                    acquired[i] = time.monotonic() - t0
                kb._exec_true(cluster, member.sandbox_name, args.namespace)
                with lock:
                    starts[i] = time.monotonic() - t0
                step_end.wait()
                fill["batch"].release_member(member)
                return
            if sem is not None:
                with sem:
                    handle = fleet.acquire(task)
            else:
                handle = fleet.acquire(task)
            with lock:
                acquired[i] = time.monotonic() - t0
            handle.exec(["true"])
            with lock:
                starts[i] = time.monotonic() - t0
            step_end.wait()
            fleet.release(handle)
        except Exception as e:  # noqa: BLE001 — recorded, the step carries on
            with lock:
                errors.append(f"{type(e).__name__}: {e}"[:300])

    threads = [threading.Thread(target=env, args=(i, t), daemon=True)
               for i, t in enumerate(fleet.tasks)]
    for th in threads:
        th.start()
    before = scrape_apiserver(api)
    snap = COUNTER.snapshot()
    throttled0, pool_full0 = THROTTLE.count, POOL_FULL.count
    cpu0 = cpu_seconds()
    t0 = time.monotonic()
    if variant.arm == "fill":
        from k8s_agent_sandbox import BatchEventType, BatchGroup
        groups = [BatchGroup(warmpool=e.pool, size=e.tasks) for e in fleet.plan_.entries]
        fill_queues.update({e.pool: queue.Queue() for e in fleet.plan_.entries})
        go.set()
        fill["batch"] = cluster.batch_sandbox_client().claim_batch(
            groups, namespace=args.namespace, labels=dict(fleet.config.labels),
            create_rps=variant.create_rps, max_in_flight=variant.max_in_flight,
            work_budget=3600, quorum_timeout=600, lease_duration=variant.lease_duration)

        def dispatch():
            for ev in fill["batch"].events():
                if ev.type is BatchEventType.MEMBER_READY:
                    fill_queues[ev.member.warmpool].put(ev.member)
            for q in fill_queues.values():
                for _ in range(shape.per_image):
                    q.put(None)
        threading.Thread(target=dispatch, daemon=True).start()
    else:
        go.set()
    deadline = time.monotonic() + STEP_DEADLINE_S
    while time.monotonic() < deadline:
        with lock:
            done = len(starts) + len(errors)
        if done >= n:
            break
        time.sleep(0.05)
    step_s = time.monotonic() - t0
    cost = COUNTER.since(snap)
    cpu = cpu_seconds() - cpu0
    pool_full = POOL_FULL.count - pool_full0
    with lock:
        errors.extend(["env did not start before the deadline"]
                      * (n - len(starts) - len(errors)))
    after = scrape_apiserver(api)
    t_release = time.monotonic()
    step_end.set()
    for th in threads:
        th.join(timeout=300)
    if fill.get("batch") is not None:
        fill["batch"].release()
    out = burst_out(variant, shape, n, starts, acquired, errors, step_s=step_s,
                    release_s=time.monotonic() - t_release, cpu=cpu,
                    throttled=THROTTLE.count - throttled0, cost=cost, warm={})
    out["pool_full_discards"] = pool_full
    out["apiserver"] = apiserver_delta(before, after, cost.get("creates", 0))
    return out


def run_sync(args, p: PlannedRun) -> dict:
    """One fleet, ``p.steps`` steps, the shape's state restored before each."""
    _, custom, api = k8s(args)
    scale_for(args, custom, p.shape)
    fleet = arm_fleet(args, p.variant, p.shape)
    cluster = next(iter(fleet.registry))
    steps = []
    try:
        for _ in range(p.steps):
            warm = wait_state(args, p.shape)
            s = run_step(args, fleet, cluster, api, p.variant, p.shape)
            s.update(warm)
            steps.append(s)
    finally:
        t = time.monotonic()
        fleet.teardown()
        teardown_s = time.monotonic() - t
    return {"steps": steps, "teardown_s": teardown_s}


def run_async(args, p: PlannedRun) -> dict:
    """One step through ``AsyncSandboxFleet``, the kind driver's ``run_async_step``."""
    from agent_sandbox_rl import AsyncSandboxFleet
    _, custom, api = k8s(args)
    scale_for(args, custom, p.shape)
    warm = wait_state(args, p.shape)
    fleet = AsyncSandboxFleet(sync_fleet=arm_fleet(args, p.variant, p.shape))
    exec_pool = ThreadPoolExecutor(max_workers=p.shape.tasks, thread_name_prefix="bench-exec")
    before = scrape_apiserver(api)
    pool_full0 = POOL_FULL.count
    try:
        step = asyncio.run(run_async_step(fleet, p.variant, p.shape.tasks, exec_pool,
                                          deadline_s=STEP_DEADLINE_S))
    finally:
        exec_pool.shutdown(wait=True)
    after = scrape_apiserver(api)
    s = burst_out(p.variant, p.shape, p.shape.tasks, step["starts"], step["acquired"],
                  step["errors"], step_s=step["step_s"], release_s=step["release_s"],
                  cpu=step["cpu"], throttled=step["throttled"], cost=step["cost"], warm=warm)
    s["pool_full_discards"] = POOL_FULL.count - pool_full0
    # The second scrape is after teardown here, so release requests are included.
    s["apiserver"] = apiserver_delta(before, after, step["cost"].get("creates", 0))
    s["loop_lag_s"] = step["loop_lag_s"]
    return {"steps": [s], "teardown_s": None}


def run_one(args, p: PlannedRun) -> dict:
    r = run_async(args, p) if p.variant.loop == "async" else run_sync(args, p)
    steps = r["steps"]
    r.update(variant=p.variant.name, arm=p.variant.arm, shape=p.shape.name,
             n=p.shape.tasks, warm=p.shape.warm, settings=steps[0]["settings"],
             errors=sum(s["errors"] for s in steps), steady=steady_of(steps))
    return r


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_images(args) -> int:
    for img in all_images():
        print(img)
    return 0


def cmd_setup(args) -> int:
    """Creates every pool at full replicas through the fleet (its only setup path),
    then scales them all to 0, so the cluster must hold every pool once."""
    from agent_sandbox_rl import SandboxFleet
    from kubernetes import client
    core, custom, _ = k8s(args)
    cap, used = kb.node_pod_capacity(core)
    need = max(POOLS * PER_IMAGE, 2 * max(SIZES)) + kb.POD_HEADROOM
    print(f"node pod capacity {cap}, pods scheduled {used}, setup and the largest warm "
          f"step need {need} more")
    if cap - used < need:
        print("not enough pod slots on the cluster's nodes", file=sys.stderr)
        return 1
    try:
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(
            name=args.namespace)))
        print(f"created namespace {args.namespace}")
    except client.ApiException as e:
        if e.status != 409:
            raise
    shape = Shape(POOLS * PER_IMAGE)
    f = SandboxFleet(fleet_config(args, None, shape))
    f.load_tasks([img for img in shape.image_list() for _ in range(PER_IMAGE)])
    f.setup()
    scale_for(args, custom, Shape(PER_IMAGE, warm=False))
    print(f"created {len(f.plan_.entries)} warm pools in {args.namespace}, scaled to 0")
    return 0


def _result_path(label: str) -> str:
    return os.path.join(RESULTS, f"{label}.json")


def cmd_status(args) -> int:
    lines = ["# GKE claim-mode bench status", "",
             "Regenerated from the result files on every suite start and run.", ""]
    for p in run_plan():
        path = _result_path(p.label)
        if not os.path.exists(path):
            lines.append(f"- [ ] {p.label}")
            continue
        with open(path) as f:
            r = json.load(f)
        flags = []
        if r.get("errors"):
            flags.append(f"errors={r['errors']}")
        if r.get("exception"):
            flags.append("exception")
        st = r.get("steady") or {}
        if st.get("ready_last") is not None:
            flags.append(f"ready_last={st['ready_last']:.2f}s")
        lines.append(f"- [x] {p.label} {' '.join(flags)}".rstrip())
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "STATUS.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return 0


def clean_stale(args) -> None:
    """Deletes this run's claims and Leases left by a suite that stopped mid-run."""
    from kubernetes import client

    from agent_sandbox_rl import constants
    _, custom, api = k8s(args)
    sel = f"{constants.RUN_ID_LABEL}={RUN_ID}"
    claims = kb.run_claims(custom, args)
    leases = client.CoordinationV1Api(api)
    n_leases = len(leases.list_namespaced_lease(args.namespace, label_selector=sel).items)
    if claims or n_leases:
        print(f"deleting {claims} claims and {n_leases} Leases left by an earlier run",
              flush=True)
        custom.delete_collection_namespaced_custom_object(
            constants.GROUP, constants.VERSION, args.namespace, constants.CLAIMS_PLURAL,
            label_selector=sel)
        leases.delete_collection_namespaced_lease(args.namespace, label_selector=sel)


def cmd_suite(args) -> int:
    COUNTER.install()
    THROTTLE.install()
    POOL_FULL.install()
    os.makedirs(RESULTS, exist_ok=True)
    cmd_status(args)
    prov = provenance()
    if prov["dirty"] and not args.allow_dirty:
        print("the checkout has uncommitted changes; commit them or pass --allow-dirty",
              file=sys.stderr)
        return 1
    clean_stale(args)
    for p in run_plan():
        if args.only and not any(p.label.startswith(o) for o in args.only):
            continue
        path = _result_path(p.label)
        if os.path.exists(path):
            continue
        print(f"{time.strftime('%H:%M:%S')} {p.label}", flush=True)
        t0 = time.time()
        try:
            r = run_one(args, p)
        except Exception as e:  # noqa: BLE001 — recorded so the suite moves on
            r = {"exception": f"{type(e).__name__}: {e}"[:1000], "errors": 1,
                 "variant": p.variant.name, "shape": p.shape.name}
            clean_stale(args)
        r.update(label=p.label, run=p.run, wall_started=t0, wall_s=time.time() - t0,
                 provenance=prov)
        with open(path, "w") as f:
            json.dump(r, f, indent=1)
        cmd_status(args)
    return 0


def _load() -> list[dict]:
    out = []
    for p in run_plan():
        path = _result_path(p.label)
        if os.path.exists(path):
            with open(path) as f:
                out.append(json.load(f))
    return out


def _fmt(v, digits=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def cmd_report(args) -> int:
    rows = summarize(_load())
    v = verdict(rows)
    lines = ["# GKE claim-mode bench summary", "",
             f"Steady state is the median over steps 2 to {STEPS} of each run, then the "
             "median over runs. Ratios are batch over per-claim: under 1 means batch is "
             "faster. Ready is when fleet.acquire returned; start adds the one exec.", "",
             "## Verdict", "", "```", json.dumps(v, indent=1), "```", "",
             "## Steady state", "",
             "| shape/variant | runs | errors | last Ready s (min-max) | Ready p50 s | "
             "Ready p99 s | last start s | step 1 last Ready s | requests/env | watches | "
             "pool-full discards | CPU s | API create ms | APF wait s | API claim-path requests "
             "| all API requests per s (context) | 429s |", "|" + "---|" * 17]
    for key in sorted(rows, key=lambda k: (k[0] != "w", int(k.split("/")[0][1:]), k)):
        r = rows[key]
        lines.append(
            f"| {key} | {r['runs']} | {r['errors']} | {_fmt(r['ready_last'])} "
            f"({_fmt(r['ready_last_min'])}-{_fmt(r['ready_last_max'])}) | "
            f"{_fmt(r['ready_p50'])} | {_fmt(r['ready_p99'])} | {_fmt(r['last_start_s'])} | "
            f"{_fmt(r['step1_ready_last'])} | "
            f"{_fmt(ratio(r['total_requests'], r['n']))} | {_fmt(r['watches'], 0)} | "
            f"{_fmt(r['pool_full_discards'], 0)} | {_fmt(r['driver_cpu_s'])} | "
            f"{_fmt(r['apiserver_create_ms'])} | {_fmt(r['apiserver_apf_wait_s'])} | "
            f"{_fmt(r['apiserver_claim_path_requests'], 0)} | "
            f"{_fmt(ratio(r['apiserver_requests_all'], r['last_start_s']), 0)} | "
            f"{_fmt(r['apiserver_http_429_all'], 0)} |")
    lines += ["", "Requests and watches are the driver's own, from the step's start to its "
              "last exec, before release. API columns are the API server's counters over the "
              "same window, from steps whose claim creates matched the driver's. Claim-path "
              "requests are sandboxclaims, sandboxes and leases from every client (the "
              "controller included), the arm's API cost. All API requests per second covers "
              "every client in the cluster (kubelets and refills too), so it leans against "
              "the slower arm: context only.", ""]
    with open(os.path.join(RESULTS, "summary.md"), "w") as f:
        f.write("\n".join(lines))
    print(os.path.join(RESULTS, "summary.md"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--context", default=None,
                   help="kube context; ignored with --in-cluster")
    p.add_argument("--in-cluster", action="store_true",
                   help="use the pod's service account (the driver runs in the cluster)")
    p.add_argument("--namespace", default=NAMESPACE)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("images")
    sub.add_parser("setup")
    s = sub.add_parser("suite")
    s.add_argument("--only", nargs="*", default=None,
                   help="label prefixes to run, for example w160- or c640-batch")
    s.add_argument("--allow-dirty", action="store_true")
    sub.add_parser("status")
    sub.add_parser("report")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return {"images": cmd_images, "setup": cmd_setup, "suite": cmd_suite,
            "status": cmd_status, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
