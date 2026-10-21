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

"""Benchmarks `FleetConfig.claim_mode` on a kind cluster: per-claim against batch.

Not a unit test (it needs a live cluster); the filename keeps pytest from
collecting it. Pure helpers are unit-tested in ``test_claim_mode_bench.py``.

Every arm runs from this one checkout, so the per-claim and batch arms differ only
in ``claim_mode`` and its settings. Each starts from the same warm state: every
pool of its shape at full ``readyReplicas``, no claims, no terminating pods.

Arms, per env (a tunix-like loop: acquire on reset, one exec, release on close):
  per-claim    ``FleetConfig(claim_mode="per_claim")``, optionally with at most
               ``acquire_concurrency`` acquires in flight, to separate the gain
               from batching from the gain from bounded concurrency
  batch        ``FleetConfig(claim_mode="batch")``, the code under test
  fill         one ``claim_batch`` for the whole step, one group per pool sized to
               its tasks; envs take Ready members as ``events()`` yields them.
               Test-only: it is the deferred ``fleet.prefetch`` hook's mechanism

Subcommands:
  images   tag the loaded source image once per task image on every kind node
  setup    create the namespace, templates and warm pools for every shape
  suite    run every planned run that has no result file yet, in plan order
  status   regenerate STATUS.md from the result files
  report   write summary.md (tables and the verdict rules) from the result files
  worker   one subprocess driver for the Lease and failure steps; started by suite

Each run writes ``<results>/<label>.json`` once it finishes and never overwrites
one, so a stopped suite resumes where it left off. Runs of one step are
interleaved (run 1 of every variant, then run 2, ...) with the variant order
rotated each round, so drift in the cluster spreads evenly across variants.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import queue
import random
import resource
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

NAMESPACE = "rl-bench"
CONTEXT = "kind-batch-bench"
KIND_CLUSTER = "batch-bench"
RUN_ID = "rlbench"
SOURCE_IMAGE = "docker.io/library/python:3.12-slim"
IMAGE_REPO = "bench.local/rl-task"
TEMPLATE_PREFIX = "rlb-"
HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
RESULTS = os.environ.get("RL_BENCH_RESULTS",
                         os.path.join(PKG, "bench-results", "claim-mode"))
SMOKE = os.environ.get("RL_BENCH_SMOKE") == "1"
# Exits at once on SIGTERM, so released pods don't hold node slots for the
# 30 s grace period (sleep as PID 1 ignores SIGTERM).
KEEPALIVE = ["bash", "-c", "trap 'exit 0' TERM; sleep infinity & wait"]
# Node pod slots kept free for system pods and churn.
POD_HEADROOM = 20


# --------------------------------------------------------------------------- #
# Shapes and the run plan (pure; unit-tested)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Shape:
    """``images`` distinct task images, each with ``per_image`` tasks and a warm
    pool of ``per_image`` replicas."""

    name: str
    prefix: str
    images: int
    per_image: int

    def image(self, i: int) -> str:
        return f"{IMAGE_REPO}:{self.prefix}{i:02d}"

    def image_list(self) -> list[str]:
        return [self.image(i) for i in range(self.images)]

    @property
    def tasks(self) -> int:
        return self.images * self.per_image


BURST = Shape("b160", "b", 40, 4) if not SMOKE else Shape("b160", "b", 2, 4)
ROLLING = Shape("r80", "r", 10, 8) if not SMOKE else Shape("r80", "r", 2, 4)
ONDEMAND_IMAGE = f"{IMAGE_REPO}:od0"
SHAPES = {s.name: s for s in (BURST, ROLLING)}


@dataclass(frozen=True)
class Variant:
    name: str
    arm: str                                  # per-claim | batch | fill
    create_rps: float | None = None
    max_in_flight: int | None = None
    acquire_concurrency: int | None = None    # per-claim only
    lease_duration: int = 300


# Every claim here is covered by a warm pool, so the SDK sends it unpaced and only
# max_in_flight bounds a burst; create_rps applies only after a 429.
BURST_VARIANTS = [
    Variant("per-claim", "per-claim"),
    Variant("per-claim-c20", "per-claim", acquire_concurrency=20),
    Variant("per-claim-c50", "per-claim", acquire_concurrency=50),
    Variant("batch-default", "batch"),
    Variant("batch-mif50", "batch", max_in_flight=50),
    Variant("batch-mif100", "batch", max_in_flight=100),
    Variant("batch-mif200", "batch", max_in_flight=200),
    Variant("fill-default", "fill"),
    Variant("fill-mif50", "fill", max_in_flight=50),
]
ROLLING_VARIANTS = [
    Variant("per-claim", "per-claim"),
    Variant("batch-default", "batch"),
    Variant("batch-mif50", "batch", max_in_flight=50),
]
LEASE_VARIANTS = [
    Variant("lease60", "batch", lease_duration=60),
    Variant("lease300", "batch", lease_duration=300),
]
COMPOSITION = ["recycle", "ondemand", "adopt", "maxlive", "pin"]
FAILURES = ["sigkill-reaper", "sigkill-noreaper", "stall", "partition",
            "member-deleted"]
LONG = ["recycle-expiry"]

BURST_RUNS = 1 if SMOKE else 3
ROLLING_RUNS = 1 if SMOKE else 2
ROLLING_SECONDS = 30 if SMOKE else 240
ROLLING_CONCURRENCY = 4 if SMOKE else 40
LEASE_WORKERS = 2 if SMOKE else 8
# The SDK renews every lease_duration // 3 s, the first one interval after the
# batch opens: 130 s holds one 300 s-Lease renewal, 420 s holds four.
LEASE_SECONDS = 130 if SMOKE else 420
FAILURE_LEASE = 30


@dataclass(frozen=True)
class PlannedRun:
    step: str
    label: str
    variant: Variant | None = None
    run: int = 1
    shape: str | None = None
    case: str | None = None


def interleave(variants: list[Variant], runs: int) -> list[tuple[Variant, int]]:
    """Run 1 of every variant, then run 2, ...; each round starts one variant
    later than the last, so no variant always runs first after a settle."""
    order = []
    n = len(variants)
    for k in range(1, runs + 1):
        shift = (k - 1) % n if n else 0
        for v in variants[shift:] + variants[:shift]:
            order.append((v, k))
    return order


def run_plan() -> list[PlannedRun]:
    plan = []
    for v, k in interleave(BURST_VARIANTS, BURST_RUNS):
        plan.append(PlannedRun("burst", f"burst-{BURST.name}-{v.name}-run{k}", v, k, BURST.name))
    for v, k in interleave(ROLLING_VARIANTS, ROLLING_RUNS):
        plan.append(PlannedRun("rolling", f"rolling-{ROLLING.name}-{v.name}-run{k}", v, k,
                               ROLLING.name))
    for v in LEASE_VARIANTS:
        plan.append(PlannedRun("lease", f"lease-{v.name}", v, 1, ROLLING.name))
    for case in COMPOSITION:
        plan.append(PlannedRun("composition", f"comp-{case}", shape=ROLLING.name, case=case))
    for case in FAILURES:
        plan.append(PlannedRun("failure", f"fail-{case}", shape=ROLLING.name, case=case))
    for case in LONG:
        plan.append(PlannedRun("long", f"long-{case}", shape=ROLLING.name, case=case))
    return plan


# --------------------------------------------------------------------------- #
# Measurement helpers (pure; unit-tested)
# --------------------------------------------------------------------------- #
def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def dist(values):
    if not values:
        return None
    return {"n": len(values), "first": min(values), "p50": percentile(values, 50),
            "p90": percentile(values, 90), "p99": percentile(values, 99),
            "last": max(values)}


def classify_request(method: str, path: str, query: dict) -> str:
    """Maps a Kubernetes API request to a 'verb resource[/subresource]' key."""
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["api"]:
        rest = parts[2:]
    elif parts[:1] == ["apis"]:
        rest = parts[3:]
    else:
        return f"{method.lower()} {path}"
    if rest[:1] == ["namespaces"] and len(rest) >= 3:
        rest = rest[2:]
    res = rest[0] if rest else "?"
    name = rest[1] if len(rest) > 1 else None
    sub = rest[2] if len(rest) > 2 else None
    method = method.upper()
    if method == "GET":
        watch = str(query.get("watch", "")).lower() in ("true", "1")
        verb = "get" if name else ("watch" if watch else "list")
    elif method == "DELETE":
        verb = "delete" if name else "deletecollection"
    else:
        verb = {"POST": "create", "PUT": "update", "PATCH": "patch"}.get(method, method.lower())
    return f"{verb} {res}{'/' + sub if sub else ''}"


def group_starts(starts: dict[int, float], per_group: int) -> list[float]:
    """A group starts when its last member has started (GRPO waits for all G).
    ``starts`` maps a task index (grouped consecutively by image) to its start."""
    groups: dict[int, list[float]] = {}
    for i, t in starts.items():
        groups.setdefault(i // per_group, []).append(t)
    return sorted(max(ts) for ts in groups.values() if len(ts) == per_group)


def lognormal_holds(n: int, median: float, p99_ratio: float, cap: float,
                    seed: int) -> list[float]:
    """``n`` hold times with the given median and p99/median ratio, capped. The
    same seed gives every arm of a run the same holds."""
    sigma = math.log(p99_ratio) / 2.326
    rng = random.Random(seed)
    return [min(cap, median * math.exp(rng.gauss(0, sigma))) for _ in range(n)]


def median_of(results: list[dict], key: str):
    vals = [r[key] for r in results if r.get(key) is not None]
    return statistics.median(vals) if vals else None


def ratio(a, b):
    return None if a is None or not b else a / b


# --------------------------------------------------------------------------- #
# Request counting (process-wide)
# --------------------------------------------------------------------------- #
class RequestCounter:
    """Counts every request the sync Kubernetes client sends, by verb and resource,
    with the time of each claim create and every non-2xx status. Exec goes over a
    websocket that bypasses ApiClient.call_api, so the env loop counts execs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.counts: dict[str, int] = {}
        self.statuses: dict[str, int] = {}
        self.create_times: list[float] = []

    def install(self) -> None:
        from kubernetes.client import ApiClient
        counter = self
        orig = ApiClient.call_api

        def call_api(self, method, url, *args, **kwargs):
            parsed = urlparse(url)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            key = classify_request(method, parsed.path, query)
            now = time.monotonic()
            try:
                resp = orig(self, method, url, *args, **kwargs)
            except Exception as e:
                # The client raises ApiException for every non-2xx response, so the
                # HTTP status (a 429 among them) is on the exception. -1 means the
                # request got no HTTP response (reset, timeout).
                counter.add(key, getattr(e, "status", None) or -1, now)
                raise
            counter.add(key, getattr(resp, "status", 0) or 0, now)
            return resp

        ApiClient.call_api = call_api

    def add(self, key: str, status: int, now: float) -> None:
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            if key == "create sandboxclaims":
                self.create_times.append(now)
            if not 200 <= status < 300:
                s = str(status)
                self.statuses[s] = self.statuses.get(s, 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(self.counts), dict(self.statuses), len(self.create_times)

    def since(self, snap) -> dict:
        counts0, statuses0, creates0 = snap
        with self.lock:
            counts = {k: v - counts0.get(k, 0) for k, v in self.counts.items()
                      if v - counts0.get(k, 0)}
            statuses = {k: v - statuses0.get(k, 0) for k, v in self.statuses.items()
                        if v - statuses0.get(k, 0)}
            creates = self.create_times[creates0:]
        out = {"requests": counts, "total_requests": sum(counts.values()),
               "watches": sum(v for k, v in counts.items() if k.startswith("watch ")),
               "statuses": statuses, "http_429": statuses.get("429", 0),
               "creates": len(creates)}
        if len(creates) >= 2:
            # Times are taken when a create is sent but appended when it returns, so
            # concurrent creates land out of order.
            window = max(creates) - min(creates)
            out["create_window_s"] = window
            out["creates_per_s"] = (len(creates) - 1) / window if window > 0 else None
        return out


COUNTER = RequestCounter()


class ThrottleLog(logging.Handler):
    """Counts the SDK's one-time "was throttled" log: after a 429 a batch paces
    every later create at create_rps."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.count = 0

    def emit(self, record):
        if "was throttled" in record.getMessage():
            self.count += 1

    def install(self) -> None:
        sdk = logging.getLogger("k8s_agent_sandbox")
        sdk.addHandler(self)
        if sdk.getEffectiveLevel() > logging.INFO:
            sdk.setLevel(logging.INFO)


THROTTLE = ThrottleLog()


def cpu_seconds() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def max_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


# --------------------------------------------------------------------------- #
# Cluster plumbing
# --------------------------------------------------------------------------- #
def _cluster_config(args):
    from agent_sandbox_rl import ClusterConfig
    return ClusterConfig(name="kind", context=args.context, namespace=args.namespace)


def fleet_config(args, variant: Variant | None = None, **overrides):
    from agent_sandbox_rl import (BatchClaimConfig, FleetConfig, ObservabilityConfig,
                                  ResourceSpec, TemplateSpec)
    claim_mode = "batch" if variant is not None and variant.arm == "batch" else "per_claim"
    batch = BatchClaimConfig(
        create_rps=variant.create_rps if variant else None,
        max_in_flight=variant.max_in_flight if variant else None,
        lease_duration=variant.lease_duration if variant else 300)
    cfg = dict(
        clusters=[_cluster_config(args)], run_id=RUN_ID,
        template_name_prefix=TEMPLATE_PREFIX, warm_per_task=True,
        max_warmpool_size=8, max_concurrent=max(BURST.tasks, ROLLING.tasks),
        ready_timeout=300, claim_mode=claim_mode, batch_claims=batch,
        # The breaker counts this run's pods, warm ones included, against one
        # arm's plan; the bench bounds its own pods instead.
        overcommit_factor=0, install_teardown_hooks=False,
        observability=ObservabilityConfig(enable_metrics=False),
        template=TemplateSpec(keepalive_command=list(KEEPALIVE),
                              resources=ResourceSpec(cpu="10m", memory="8Mi")))
    cfg.update(overrides)
    return FleetConfig(**cfg)


def arm_fleet(args, variant: Variant | None, images: list[str], per_image: int,
              registry=None, **overrides):
    """A fleet over pools `setup` created: it plans the same names and creates
    nothing, and under the shared run id its teardown deletes no pool."""
    from agent_sandbox_rl import SandboxFleet
    f = SandboxFleet(fleet_config(args, variant, **overrides), registry=registry)
    f.load_tasks([img for img in images for _ in range(per_image)])
    f.plan()
    return f


def k8s(args):
    from agent_sandbox_rl.cluster import build_api_client
    from kubernetes import client
    api = build_api_client(_cluster_config(args))
    return client.CoreV1Api(api), client.CustomObjectsApi(api), api


def pools_of(args, shape: Shape) -> list[str]:
    cfg = fleet_config(args)
    return [cfg.pool_name(img) for img in shape.image_list()]


def pool_states(custom, args, pools: list[str]) -> dict[str, tuple[int, int]]:
    from agent_sandbox_rl import constants
    out = {}
    for p in pools:
        obj = custom.get_namespaced_custom_object(
            constants.GROUP, constants.VERSION, args.namespace,
            constants.WARMPOOLS_PLURAL, p)
        out[p] = (int((obj.get("spec") or {}).get("replicas") or 0),
                  int((obj.get("status") or {}).get("readyReplicas") or 0))
    return out


def scale_pools(custom, args, shape: Shape, replicas: int) -> None:
    from agent_sandbox_rl import constants
    for p in pools_of(args, shape):
        custom.patch_namespaced_custom_object(
            constants.GROUP, constants.VERSION, args.namespace,
            constants.WARMPOOLS_PLURAL, p, {"spec": {"replicas": replicas}})


def run_claims(custom, args) -> int:
    from agent_sandbox_rl import constants
    sel = f"{constants.RUN_ID_LABEL}={RUN_ID}"
    return len(custom.list_namespaced_custom_object(
        constants.GROUP, constants.VERSION, args.namespace, constants.CLAIMS_PLURAL,
        label_selector=sel).get("items", []))


def pod_counts(core, args) -> tuple[int, int]:
    """(pods in the bench namespace, of which terminating)."""
    pods = core.list_namespaced_pod(args.namespace).items
    return len(pods), sum(1 for p in pods if p.metadata.deletion_timestamp)


def node_pod_capacity(core) -> tuple[int, int]:
    """(allocatable pods summed over nodes, pods scheduled anywhere)."""
    nodes = core.list_node().items
    cap = sum(int(n.status.allocatable.get("pods", "0")) for n in nodes)
    used = len(core.list_pod_for_all_namespaces(field_selector="spec.nodeName!=").items)
    return cap, used


def wait_warm(args, shape: Shape, timeout: float = 900) -> dict:
    """Blocks until every pool of ``shape`` is at full readyReplicas, no claim of
    this run exists and no pod in the namespace is terminating, then settles."""
    core, custom, _ = k8s(args)
    pools = pools_of(args, shape)
    t0 = time.monotonic()
    while True:
        states = pool_states(custom, args, pools)
        claims = run_claims(custom, args)
        total, terminating = pod_counts(core, args)
        full = all(ready >= spec == shape.per_image for spec, ready in states.values())
        if full and claims == 0 and terminating == 0:
            break
        if time.monotonic() - t0 > timeout:
            raise RuntimeError(f"warm state not reached in {timeout}s: claims={claims} "
                               f"terminating={terminating} pools={states}")
        time.sleep(2)
    time.sleep(5)
    return {"warm_wait_s": time.monotonic() - t0, "pods_in_namespace": total,
            "pools": len(pools), "ready_per_pool": shape.per_image}


def activate(args, shape: Shape) -> None:
    """Scales ``shape``'s pools to full and every other shape's to 0, so one node's
    pod slots hold one shape plus its refills."""
    _, custom, _ = k8s(args)
    for other in SHAPES.values():
        scale_pools(custom, args, other, other.per_image if other is shape else 0)


def provenance() -> dict:
    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=PKG, check=True, capture_output=True,
                                  text=True).stdout.strip()
        except Exception as e:  # noqa: BLE001 — record what failed instead
            return f"error: {e}"
    import k8s_agent_sandbox
    import agent_sandbox_rl
    return {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}"),
            "dirty": bool(git("status", "--porcelain", "--", ".", "../../clients",
                              ":(exclude)bench-results")),
            "sdk_file": k8s_agent_sandbox.__file__,
            "rl_file": agent_sandbox_rl.__file__, "smoke": SMOKE,
            "python": sys.version.split()[0]}


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #
def _exec_true(cluster, pod: str, namespace: str) -> None:
    from agent_sandbox_rl.handles import exec_in_pod
    exec_in_pod(cluster.exec_core_api(), pod, namespace, ["true"])


def run_burst(args, variant: Variant, shape: Shape) -> dict:
    """One synchronous step: every env acquires at once, execs once, waits for the
    step to end, then releases."""
    activate(args, shape)
    warm = wait_warm(args, shape)
    images = shape.image_list()
    n = shape.tasks
    starts: dict[int, float] = {}
    acquired: dict[int, float] = {}
    errors: list[str] = []
    lock = threading.Lock()
    go = threading.Event()
    step_end = threading.Event()
    fleet = arm_fleet(args, variant, images, shape.per_image)
    cluster = next(iter(fleet.registry))
    sem = (threading.Semaphore(variant.acquire_concurrency)
           if variant.acquire_concurrency else None)
    fill_queues: dict[str, queue.Queue] = {}
    fill_batch = None

    def env(i: int, task):
        try:
            go.wait()
            if variant.arm == "fill":
                pool = fleet.plan_.for_image(task.image).pool
                member = fill_queues[pool].get(timeout=600)
                if member is None:
                    raise RuntimeError("fill ended before this env got a member")
                with lock:
                    acquired[i] = time.monotonic() - t0
                _exec_true(cluster, member.sandbox_name, args.namespace)
                with lock:
                    starts[i] = time.monotonic() - t0
                step_end.wait()
                fill_batch.release_member(member)
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
    snap = COUNTER.snapshot()
    throttled0 = THROTTLE.count
    cpu0 = cpu_seconds()
    t0 = time.monotonic()
    if variant.arm == "fill":
        from k8s_agent_sandbox import BatchEventType, BatchGroup
        groups = [BatchGroup(warmpool=e.pool, size=e.tasks) for e in fleet.plan_.entries]
        fill_queues.update({e.pool: queue.Queue() for e in fleet.plan_.entries})
        go.set()
        fill_batch = cluster.batch_sandbox_client().claim_batch(
            groups, namespace=args.namespace, labels=dict(fleet.config.labels),
            create_rps=variant.create_rps, max_in_flight=variant.max_in_flight,
            work_budget=3600, quorum_timeout=300, lease_duration=variant.lease_duration)
        claim_batch_s = time.monotonic() - t0

        def dispatch():
            for ev in fill_batch.events():
                # A failed or lost member is not forwarded, so its env waits out
                # the 600 s deadline and records an error.
                if ev.type is BatchEventType.MEMBER_READY:
                    fill_queues[ev.member.warmpool].put(ev.member)
            for q in fill_queues.values():   # wake envs whose member never came
                for _ in range(shape.per_image):
                    q.put(None)
        threading.Thread(target=dispatch, daemon=True).start()
    else:
        go.set()
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        with lock:
            done = len(starts) + len(errors)
        if done >= n:
            break
        time.sleep(0.05)
    step_s = time.monotonic() - t0
    cost = COUNTER.since(snap)
    cpu = cpu_seconds() - cpu0
    t_release = time.monotonic()
    step_end.set()
    for th in threads:
        th.join(timeout=300)
    if fill_batch is not None:
        fill_batch.release()
    fleet.teardown()
    release_s = time.monotonic() - t_release
    start_times = list(starts.values())
    groups = group_starts(starts, shape.per_image)
    out = {
        "variant": variant.name, "arm": variant.arm, "shape": shape.name, "n": n,
        "settings": {"create_rps": variant.create_rps, "max_in_flight": variant.max_in_flight,
                     "acquire_concurrency": variant.acquire_concurrency,
                     "lease_duration": variant.lease_duration},
        "started": len(starts), "errors": len(errors), "error_samples": errors[:5],
        "acquired_s": dist(list(acquired.values())), "start_s": dist(start_times),
        "first_start_s": min(start_times) if start_times else None,
        "median_start_s": percentile(start_times, 50),
        "last_start_s": max(start_times) if start_times else None,
        "group_start_s": dist(groups),
        "first_group_s": groups[0] if groups else None,
        "last_group_s": groups[-1] if groups else None,
        "step_s": step_s, "release_s": release_s, "driver_cpu_s": cpu,
        "max_rss_mb": max_rss_mb(), "execs": len(starts),
        "throttled_logs": THROTTLE.count - throttled0, **cost, **warm,
    }
    if variant.arm == "fill":
        out["claim_batch_s"] = claim_batch_s
    return out


def run_rolling(args, variant: Variant, shape: Shape, run: int) -> dict:
    """``ROLLING_CONCURRENCY`` envs loop acquire, exec, hold, release for
    ``ROLLING_SECONDS``. Holds are lognormal (median 15 s, p99 10x, capped at
    150 s) and seeded by run, so every arm of a run gets the same holds."""
    activate(args, shape)
    warm = wait_warm(args, shape)
    images = shape.image_list()
    fleet = arm_fleet(args, variant, images, shape.per_image)
    _, custom, _ = k8s(args)
    pools = pools_of(args, shape)
    holds = lognormal_holds(10000, 15.0, 10.0, 150.0, seed=1000 + run)
    hold_i = iter(range(len(holds)))
    lat: list[float] = []
    done = 0
    errors: list[str] = []
    lock = threading.Lock()
    stop = time.monotonic() + ROLLING_SECONDS
    min_ready = {"value": None}

    def sample():
        while time.monotonic() < stop:
            try:
                ready = sum(r for _, r in pool_states(custom, args, pools).values())
                cur = min_ready["value"]
                min_ready["value"] = ready if cur is None else min(cur, ready)
            except Exception:  # noqa: BLE001 — a missed sample isn't a result
                pass
            time.sleep(5)

    def env(w: int):
        nonlocal done
        k = w
        while time.monotonic() < stop:
            task = fleet.tasks[k % len(fleet.tasks)]
            k += ROLLING_CONCURRENCY
            with lock:
                hold = holds[next(hold_i) % len(holds)]
            try:
                a0 = time.monotonic()
                h = fleet.acquire(task)
                with lock:
                    lat.append(time.monotonic() - a0)
                h.exec(["true"])
                time.sleep(min(hold, max(0.0, stop - time.monotonic())))
                fleet.release(h)
                with lock:
                    done += 1
            except Exception as e:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(e).__name__}: {e}"[:300])

    snap = COUNTER.snapshot()
    throttled0 = THROTTLE.count
    cpu0 = cpu_seconds()
    t0 = time.monotonic()
    threading.Thread(target=sample, daemon=True).start()
    workers = [threading.Thread(target=env, args=(w,), daemon=True)
               for w in range(ROLLING_CONCURRENCY)]
    for th in workers:
        th.start()
    for th in workers:
        th.join()
    elapsed = time.monotonic() - t0
    cost = COUNTER.since(snap)
    cpu = cpu_seconds() - cpu0
    fleet.teardown()
    return {
        "variant": variant.name, "arm": variant.arm, "shape": shape.name,
        "concurrency": ROLLING_CONCURRENCY, "seconds": elapsed,
        "settings": {"create_rps": variant.create_rps, "max_in_flight": variant.max_in_flight},
        "rollouts": done, "rollouts_per_min": done / elapsed * 60 if elapsed else None,
        "acquire_s": dist(lat), "errors": len(errors), "error_samples": errors[:5],
        "requests_per_rollout": cost["total_requests"] / done if done else None,
        "min_warm_ready": min_ready["value"],
        "starved": min_ready["value"] == 0, "driver_cpu_s": cpu,
        "max_rss_mb": max_rss_mb(), "throttled_logs": THROTTLE.count - throttled0,
        **cost, **warm,
    }


# --------------------------------------------------------------------------- #
# Subprocess workers (Lease writes and failure steps)
# --------------------------------------------------------------------------- #
class Partition:
    """A TCP forwarder to the apiserver whose traffic can be cut, so one driver
    loses the apiserver while the cluster keeps running."""

    def __init__(self, target_host: str, target_port: int):
        self.target = (target_host, target_port)
        self.cut = threading.Event()
        self.lock = threading.Lock()
        self.socks: list[socket.socket] = []
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(512)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            conn, _ = self.listener.accept()
            if self.cut.is_set():
                conn.close()
                continue
            try:
                up = socket.create_connection(self.target, timeout=10)
            except OSError:
                conn.close()
                continue
            with self.lock:
                self.socks += [conn, up]
            for a, b in ((conn, up), (up, conn)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass

    def cut_for(self, seconds: float) -> None:
        self.cut.set()
        with self.lock:
            socks, self.socks = self.socks, []
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
                s.close()
            except OSError:
                pass
        time.sleep(seconds)
        self.cut.clear()


def cmd_worker(args) -> int:
    """Drives one batch-mode fleet in its own process and writes a JSON report."""
    from agent_sandbox_rl import ClusterRegistry
    from agent_sandbox_rl.cluster import Cluster, build_api_client
    COUNTER.install()
    THROTTLE.install()
    variant = Variant("worker", "batch", lease_duration=args.lease)
    shape = SHAPES[args.shape]
    cc = _cluster_config(args)
    api = build_api_client(cc)
    partition = None
    if args.partition_after is not None:
        u = urlparse(api.configuration.host)
        partition = Partition(u.hostname, u.port or 443)
        api.configuration.host = f"https://127.0.0.1:{partition.port}"
        from kubernetes import client
        api = client.ApiClient(api.configuration)
    registry = ClusterRegistry([Cluster(cc, api_client=api)])
    fleet = arm_fleet(args, variant, shape.image_list(), shape.per_image, registry=registry)
    report: dict = {"mode": args.mode, "lease": args.lease, "pid": os.getpid()}
    t0 = time.monotonic()
    if args.mode == "lease":
        # A slow rolling loop: Lease renewals against claim requests over time.
        stop = t0 + args.seconds
        done = 0
        errors = 0
        lock = threading.Lock()

        def env(w):
            nonlocal done, errors
            k = w
            while time.monotonic() < stop:
                try:
                    h = fleet.acquire(fleet.tasks[k % len(fleet.tasks)])
                    h.exec(["true"])
                    time.sleep(3)
                    fleet.release(h)
                    with lock:
                        done += 1
                except Exception:  # noqa: BLE001
                    with lock:
                        errors += 1
                k += 2
        ths = [threading.Thread(target=env, args=(w,)) for w in range(2)]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        cost = COUNTER.since(({}, {}, 0))
        fleet.teardown()
        report.update(rollouts=done, errors=errors, seconds=time.monotonic() - t0,
                      throttled_logs=THROTTLE.count, **cost)
    else:
        handles = [fleet.acquire(t) for t in fleet.tasks[:args.n]]
        print(json.dumps({"ready": len(handles)}), flush=True)
        if partition is not None:
            time.sleep(args.partition_after)
            partition.cut_for(args.partition_for)
        else:
            # Waits here while the parent kills or stops this process.
            time.sleep(args.hold)
        batch = handles[0]._batch_claim.batch
        # The renewal thread notices the lost Lease on its next attempt, a renew
        # interval (lease // 3) after it resumes at most.
        deadline = time.monotonic() + args.lease
        while batch.err() is None and time.monotonic() < deadline:
            time.sleep(0.5)
        report["err"] = repr(batch.err())
        report["err_seen_s"] = time.monotonic() - t0
        exec_fail = 0
        for h in handles:
            try:
                h.exec(["true"])
            except Exception:  # noqa: BLE001
                exec_fail += 1
        report["exec_failures_after"] = exec_fail
        try:
            h2 = fleet.acquire(fleet.tasks[args.n])
            report["new_acquire"] = "ok"
            report["new_batch"] = h2._batch_claim.batch.batch_id != batch.batch_id
            fleet.release(h2)
        except Exception as e:  # noqa: BLE001
            report["new_acquire"] = f"{type(e).__name__}: {e}"[:300]
        for h in handles:
            try:
                fleet.release(h)
            except Exception:  # noqa: BLE001
                pass
        fleet.teardown()
    with open(args.report, "w") as f:
        json.dump(report, f, indent=1)
    return 0


def _spawn_worker(args, mode: str, report: str, **opts) -> subprocess.Popen:
    cmd = [sys.executable, os.path.abspath(__file__), "--context", args.context,
           "--namespace", args.namespace, "worker", "--mode", mode, "--report", report]
    for k, v in opts.items():
        cmd += [f"--{k.replace('_', '-')}", str(v)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=sys.stderr, text=True,
                            cwd=PKG, env={**os.environ, "PYTHONUNBUFFERED": "1"})


def _wait_ready(proc: subprocess.Popen, timeout: float = 600) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line.startswith("{"):
            return json.loads(line)
        if proc.poll() is not None:
            raise RuntimeError(f"worker exited {proc.returncode} before ready")
    raise RuntimeError("worker not ready in time")


class ReaperLoop:
    """Runs the batch reaper every ``interval`` seconds, like a CronJob would."""

    def __init__(self, args, interval: float = 5.0):
        from agent_sandbox_rl.cluster import build_api_client
        from k8s_agent_sandbox import batch_reaper, batch_utils
        from k8s_agent_sandbox.k8s_helper import K8sHelper
        self._helper = K8sHelper(api_client=build_api_client(_cluster_config(args)))
        self._reap = batch_reaper.reap
        self._margin = batch_utils.CLOCK_SKEW_MARGIN
        self._ns = args.namespace
        self._interval = interval
        self._stop = threading.Event()
        self.reaped: list[tuple[str, float]] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                reaped, _ = self._reap(self._helper, [self._ns], self._margin)
                self.reaped += [(b, time.monotonic()) for _, b in reaped]
            except Exception:  # noqa: BLE001 — the next round retries
                pass
            self._stop.wait(self._interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=60)


def _wait_claims_gone(custom, args, timeout: float) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if run_claims(custom, args) == 0:
            return time.monotonic() - t0
        time.sleep(1)
    return None


def run_lease(args, variant: Variant, shape: Shape) -> dict:
    activate(args, shape)
    warm = wait_warm(args, shape)
    os.makedirs(os.path.join(RESULTS, "workers"), exist_ok=True)
    procs = []
    for w in range(LEASE_WORKERS):
        path = os.path.join(RESULTS, "workers", f"{variant.name}-{w}.json")
        procs.append((path, _spawn_worker(args, "lease", path, lease=variant.lease_duration,
                                          seconds=LEASE_SECONDS, shape=shape.name)))
    for _, p in procs:
        p.wait(timeout=LEASE_SECONDS + 900)
    reports = []
    for path, p in procs:
        with open(path) as f:
            reports.append(json.load(f))
    lease_writes = sum(r["requests"].get("update leases", 0) + r["requests"].get("patch leases", 0)
                       for r in reports)
    rollouts = sum(r["rollouts"] for r in reports)
    hours = statistics.mean(r["seconds"] for r in reports) / 3600
    return {"variant": variant.name, "lease_duration": variant.lease_duration,
            "workers": LEASE_WORKERS, "seconds": LEASE_SECONDS, "rollouts": rollouts,
            "errors": sum(r["errors"] for r in reports),
            "lease_writes": lease_writes,
            "lease_writes_per_process_hour": lease_writes / LEASE_WORKERS / hours,
            "lease_writes_per_process_hour_analytic": 3600 / max(1, variant.lease_duration // 3),
            "requests_per_rollout": sum(r["total_requests"] for r in reports) / rollouts
            if rollouts else None,
            "workers_detail": reports, **warm}


def run_failure(args, case: str, shape: Shape) -> dict:
    activate(args, shape)
    warm = wait_warm(args, shape)
    _, custom, _ = k8s(args)
    os.makedirs(os.path.join(RESULTS, "workers"), exist_ok=True)
    path = os.path.join(RESULTS, "workers", f"fail-{case}.json")
    lease = FAILURE_LEASE
    n = 6 if SMOKE else 20               # the worker acquires one more after the fault
    out: dict = {"case": case, "lease_duration": lease, "members": n, **warm}
    if case == "member-deleted":
        return {**out, **_member_deleted(args, shape)}
    if case in ("sigkill-reaper", "sigkill-noreaper"):
        proc = _spawn_worker(args, "hold", path, lease=lease, n=n, hold=3600, shape=shape.name)
        _wait_ready(proc)
        out["claims_before"] = run_claims(custom, args)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        t_kill = time.monotonic()
        if case == "sigkill-noreaper":
            time.sleep(lease + 30)
            out["claims_after_lease_plus_30s"] = run_claims(custom, args)
        with ReaperLoop(args) as reaper:
            gone = _wait_claims_gone(custom, args, timeout=lease * 4 + 300)
        out["claims_gone_after_kill_s"] = (None if gone is None
                                           else time.monotonic() - t_kill)
        out["reaped"] = [b for b, _ in reaper.reaped]
        out["pass"] = gone is not None and (
            case == "sigkill-reaper"
            or out["claims_after_lease_plus_30s"] == out["claims_before"])
        return out
    if case == "stall":
        stop_for = lease + 30
        proc = _spawn_worker(args, "hold", path, lease=lease, n=n, hold=stop_for + 5,
                             shape=shape.name)
        _wait_ready(proc)
        with ReaperLoop(args) as reaper:
            proc.send_signal(signal.SIGSTOP)
            time.sleep(stop_for)
            out["claims_while_stopped"] = run_claims(custom, args)
            proc.send_signal(signal.SIGCONT)
            proc.wait(timeout=900)
        out["reaped"] = [b for b, _ in reaper.reaped]
    elif case == "partition":
        proc = _spawn_worker(args, "hold", path, lease=lease, n=n, shape=shape.name,
                             partition_after=2, partition_for=lease + 30)
        _wait_ready(proc)
        with ReaperLoop(args) as reaper:
            proc.wait(timeout=900)
        out["reaped"] = [b for b, _ in reaper.reaped]
    with open(path) as f:
        out["worker"] = json.load(f)
    w = out["worker"]
    # Expected: the old batch stopped, its members are gone, and a new acquire
    # opens a fresh batch.
    out["pass"] = (w.get("err") not in (None, "None") and w.get("new_acquire") == "ok"
                   and w.get("new_batch") is True)
    out["claims_left_s"] = _wait_claims_gone(custom, args, timeout=300)
    return out


def _member_deleted(args, shape: Shape) -> dict:
    from agent_sandbox_rl import constants
    from k8s_agent_sandbox import MemberState
    _, custom, _ = k8s(args)
    fleet = arm_fleet(args, Variant("m", "batch"), shape.image_list(), shape.per_image)
    hs = [fleet.acquire(t) for t in fleet.tasks[:4]]
    victim = hs[0]
    custom.delete_namespaced_custom_object(constants.GROUP, constants.VERSION, args.namespace,
                                           constants.CLAIMS_PLURAL, victim.claim_name)
    batch = victim._batch_claim.batch
    state = None
    for _ in range(120):
        m = [x for x in batch.members() if x.claim_name == victim.claim_name]
        state = m[0].state if m else None
        if state in (MemberState.LOST, None):
            break
        time.sleep(1)
    exec_error = None
    try:
        victim.exec(["true"])
    except Exception as e:  # noqa: BLE001
        exec_error = type(e).__name__
    release_error = None
    try:
        fleet.release(victim)
    except Exception as e:  # noqa: BLE001
        release_error = f"{type(e).__name__}: {e}"[:200]
    others_ok = 0
    for h in hs[1:]:
        h.exec(["true"])
        others_ok += 1
        fleet.release(h)
    fleet.teardown()
    return {"member_state": str(state), "exec_error": exec_error,
            "release_error": release_error, "others_ok": others_ok,
            "pass": state in (MemberState.LOST, None) and release_error is None
            and others_ok == 3}


class NoopReset:
    """A reset that always succeeds, so recycle keeps one claim per image."""

    def prime(self, handle):
        from agent_sandbox_rl.recycle import ResetBaseline
        return ResetBaseline(pristine_sha="noop")

    @staticmethod
    def recyclable(baseline) -> bool:
        return True

    def reset(self, handle, baseline, *, timeout: float = 5.0):
        from agent_sandbox_rl.recycle import ResetOutcome
        return ResetOutcome(clean=True, seconds=0.0)


def run_composition(args, case: str, shape: Shape) -> dict:
    from agent_sandbox_rl import FleetOvercommitError, reuse_git_restore_sandbox
    activate(args, shape)
    warm = wait_warm(args, shape)
    images = shape.image_list()
    out: dict = {"case": case, **warm}
    snap = COUNTER.snapshot()
    if case == "recycle":
        res = {}
        imgs = images[:4]
        for mode in ("per_claim", "batch"):
            v = Variant(mode, "batch" if mode == "batch" else "per-claim")
            f = arm_fleet(args, v, imgs, 4)
            s = COUNTER.snapshot()
            results = reuse_git_restore_sandbox(
                f, f.tasks, lambda t, h: h.exec(["true"]) or "ok", concurrency=4,
                reset=NoopReset(), use_session=False, scale_on_hold=False)
            res[mode] = {"tasks": len(results),
                         "ok": sum(1 for r in results if r == "ok"),
                         "creates": COUNTER.since(s)["creates"]}
            f.teardown()
        out["modes"] = res
        # One claim per image, reused for its 4 tasks.
        out["pass"] = all(r["ok"] == r["tasks"] and r["creates"] == len(imgs)
                          for r in res.values())
    elif case == "ondemand":
        f = arm_fleet(args, Variant("b", "batch"), images[:1], 1)
        from agent_sandbox_rl import Task
        h = f.acquire(Task(id="od", image=ONDEMAND_IMAGE))
        h.exec(["true"])
        out["on_demand_member"] = h._batch_claim is not None
        f.release(h)
        f.teardown()
        _, custom, _ = k8s(args)
        from agent_sandbox_rl import constants
        pool = f.config.pool_name(ONDEMAND_IMAGE)
        try:
            custom.get_namespaced_custom_object(constants.GROUP, constants.VERSION,
                                                args.namespace, constants.WARMPOOLS_PLURAL,
                                                pool)
            out["on_demand_pool_left"] = True
        except Exception:  # noqa: BLE001 — 404 expected
            out["on_demand_pool_left"] = False
        # A shared run id's teardown leaves on-demand pools to the reaper, so remove
        # this one here and keep later steps' namespace as setup left it.
        c = next(iter(f.registry))
        c.resources.delete_warmpool(pool)
        c.resources.delete_template(f.config.template_name(ONDEMAND_IMAGE))
        out["pass"] = out["on_demand_member"]
    elif case == "adopt":
        f = arm_fleet(args, Variant("b", "batch"), images[:2], 2, adopt_existing=True)
        hs = [f.acquire(t) for t in f.tasks]
        for h in hs:
            h.exec(["true"])
            f.release(h)
        f.teardown()
        out["adopted_acquires"] = len(hs)
        out["pass"] = len(hs) == len(f.tasks)
    elif case == "maxlive":
        f = arm_fleet(args, Variant("b", "batch"), images[:1], 4, max_live_sandboxes=3)
        hs = [f.acquire(t) for t in f.tasks[:3]]
        s = COUNTER.snapshot()
        try:
            f.acquire(f.tasks[3])
            out["fourth"] = "acquired"
        except FleetOvercommitError:
            out["fourth"] = "FleetOvercommitError"
        out["creates_for_fourth"] = COUNTER.since(s)["creates"]
        for h in hs:
            f.release(h)
        f.teardown()
        out["pass"] = out["fourth"] == "FleetOvercommitError" and out["creates_for_fourth"] == 0
    elif case == "pin":
        f = arm_fleet(args, Variant("b", "batch"), images[:1], 1)
        h = f.acquire(f.tasks[0], pod_annotations={"rl-bench/pin": "x"})
        h.exec(["true"])
        out["per_claim_fallback"] = h._batch_claim is None
        f.release(h)
        f.teardown()
        out["pass"] = out["per_claim_fallback"]
    out.update(COUNTER.since(snap))
    return out


def run_long(args, case: str, shape: Shape) -> dict:
    """A recycled batch claim rotated before its shutdownTime: ready_timeout 60 s
    and work_budget 1 s give each claim 661 s, and 13 tasks of 60 s on one image
    would need 780 s, so recycle has to rotate once and no task may fail."""
    from agent_sandbox_rl import BatchClaimConfig, reuse_git_restore_sandbox
    activate(args, shape)
    warm = wait_warm(args, shape)
    task_s = 5 if SMOKE else 60
    tasks = 4 if SMOKE else 13
    f = arm_fleet(args, Variant("b", "batch"), shape.image_list()[:1], tasks,
                  ready_timeout=60, batch_claims=BatchClaimConfig(work_budget=1))
    snap = COUNTER.snapshot()

    def task(t, h):
        h.exec(["true"])
        time.sleep(task_s)
        h.exec(["true"])
        return "ok"
    results = reuse_git_restore_sandbox(f, f.tasks, task, concurrency=1, reset=NoopReset(),
                                        reset_timeout=5.0, use_session=False,
                                        scale_on_hold=False)
    cost = COUNTER.since(snap)
    f.teardown()
    ok = sum(1 for r in results if r == "ok")
    return {"case": case, "tasks": tasks, "task_s": task_s, "ok": ok,
            "failures": [repr(r)[:200] for r in results if r != "ok"],
            "claims": cost["creates"], **warm,
            "pass": ok == tasks and (SMOKE or cost["creates"] == 2)}


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_images(args) -> int:
    nodes = subprocess.run(["kind", "get", "nodes", "--name", args.kind_cluster], check=True,
                           capture_output=True, text=True).stdout.split()
    images = [img for s in SHAPES.values() for img in s.image_list()] + [ONDEMAND_IMAGE]
    for node in nodes:
        for img in images:
            subprocess.run(["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "tag",
                            "--force", SOURCE_IMAGE, img], check=True, capture_output=True)
        print(f"{node}: tagged {len(images)} images from {SOURCE_IMAGE}")
    return 0


def cmd_setup(args) -> int:
    from agent_sandbox_rl import SandboxFleet
    core, _, _ = k8s(args)
    cap, used = node_pod_capacity(core)
    # The busiest step holds one shape's pool plus its refills.
    need = max(2 * s.tasks for s in SHAPES.values()) + POD_HEADROOM
    print(f"node pod capacity {cap}, pods scheduled {used}, busiest step needs {need} more")
    if cap - used < need:
        print("not enough pod slots on the cluster's nodes", file=sys.stderr)
        return 1
    from kubernetes import client
    try:
        core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(
            name=args.namespace)))
        print(f"created namespace {args.namespace}")
    except client.ApiException as e:
        if e.status != 409:
            raise
    images = []
    for s in SHAPES.values():
        images += [img for img in s.image_list() for _ in range(s.per_image)]
    f = SandboxFleet(fleet_config(args))
    f.load_tasks(images)
    f.setup()
    print(f"created {len(f.plan_.entries)} warm pools in {args.namespace}")
    return 0


def _result_path(label: str) -> str:
    return os.path.join(RESULTS, f"{label}.json")


def cmd_status(args) -> int:
    lines = ["# claim-mode bench status", "",
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
        if r.get("starved"):
            flags.append("starved")
        if "pass" in r:
            flags.append("pass" if r["pass"] else "FAIL")
        if r.get("exception"):
            flags.append("exception")
        lines.append(f"- [x] {p.label} {' '.join(flags)}".rstrip())
    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, "STATUS.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return 0


def clean_stale(args) -> None:
    """Deletes this run's claims and Leases left by a suite that stopped mid-run.
    Nothing renews or reaps them, so every later run's warm wait would time out
    until their shutdownTime (over an hour)."""
    from kubernetes import client

    from agent_sandbox_rl import constants
    _, custom, api = k8s(args)
    sel = f"{constants.RUN_ID_LABEL}={RUN_ID}"
    claims = run_claims(custom, args)
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
        shape = SHAPES[p.shape]
        print(f"{time.strftime('%H:%M:%S')} {p.label}", flush=True)
        t0 = time.time()
        try:
            if p.step == "burst":
                r = run_burst(args, p.variant, shape)
            elif p.step == "rolling":
                r = run_rolling(args, p.variant, shape, p.run)
            elif p.step == "lease":
                r = run_lease(args, p.variant, shape)
            elif p.step == "composition":
                r = run_composition(args, p.case, shape)
            elif p.step == "failure":
                r = run_failure(args, p.case, shape)
            else:
                r = run_long(args, p.case, shape)
        except Exception as e:  # noqa: BLE001 — recorded so the suite moves on
            r = {"exception": f"{type(e).__name__}: {e}"[:1000], "errors": 1}
        r.update(label=p.label, step=p.step, run=p.run, wall_started=t0,
                 wall_s=time.time() - t0, provenance=prov)
        with open(path, "w") as f:
            json.dump(r, f, indent=1)
        cmd_status(args)
    return 0


def _load(step: str) -> list[dict]:
    out = []
    for p in run_plan():
        path = _result_path(p.label)
        if p.step == step and os.path.exists(path):
            with open(path) as f:
                out.append(json.load(f))
    return out


def summarize_burst(results: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for r in results:
        if not r.get("exception"):
            by.setdefault(r["variant"], []).append(r)
    rows = {}
    for name, rs in by.items():
        last = [r["last_start_s"] for r in rs if r.get("last_start_s") is not None]
        rows[name] = {
            "runs": len(rs), "errors": sum(r.get("errors", 0) for r in rs),
            "last_start_median": median_of(rs, "last_start_s"),
            "last_start_min": min(last) if last else None,
            "last_start_max": max(last) if last else None,
            "first_start_median": median_of(rs, "first_start_s"),
            "first_group_median": median_of(rs, "first_group_s"),
            "last_group_median": median_of(rs, "last_group_s"),
            "requests_median": median_of(rs, "total_requests"),
            "watches_median": median_of(rs, "watches"),
            "creates_per_s_median": median_of(rs, "creates_per_s"),
            "create_window_median": median_of(rs, "create_window_s"),
            # Ready times leave out the exec, which on a laptop can dominate starts.
            "ready_p50_median": _median_dist(rs, "acquired_s", "p50"),
            "ready_last_median": _median_dist(rs, "acquired_s", "last"),
            "http_429": sum(r.get("http_429", 0) for r in rs),
            "throttled_logs": sum(r.get("throttled_logs", 0) for r in rs),
            "driver_cpu_median": median_of(rs, "driver_cpu_s"),
        }
    return rows


def _median_dist(results: list[dict], key: str, stat: str):
    vals = [r[key][stat] for r in results if (r.get(key) or {}).get(stat) is not None]
    return statistics.median(vals) if vals else None


def verdict(burst_rows: dict, failures: list[dict]) -> dict:
    """The rules fixed before the run (decision doc, "What the prototype should
    measure"), applied to the burst medians and the failure rows."""
    per_claim = burst_rows.get("per-claim") or {}
    base = per_claim.get("last_start_median")
    # A per-claim run that lost envs ends early and flatters the baseline.
    baseline_ok = base is not None and not per_claim.get("errors")
    batch = {k: v["last_start_median"] for k, v in burst_rows.items()
             if k.startswith("batch-") and v.get("last_start_median") is not None
             and not v.get("errors")}
    fill = {k: v["last_start_median"] for k, v in burst_rows.items()
            if k.startswith("fill-") and v.get("last_start_median") is not None
            and not v.get("errors")}
    best_batch = min(batch.items(), key=lambda kv: kv[1]) if batch else None
    best_fill = min(fill.items(), key=lambda kv: kv[1]) if fill else None
    failures_ok = bool(failures) and all(f.get("pass") for f in failures)
    out = {
        "per_claim_last_start": base,
        "batch_default_ratio": ratio(batch.get("batch-default"), base),
        "best_batch": best_batch and {"variant": best_batch[0],
                                      "ratio": ratio(best_batch[1], base)},
        "best_fill": best_fill and {"variant": best_fill[0], "ratio": ratio(best_fill[1], base)},
        "failures_all_pass": failures_ok,
        "per_claim_errors": per_claim.get("errors"),
        # Same in-flight limit on both sides. Unbounded per-claim has all 160 in
        # flight, which stands in for batch-mif100 and batch-mif200.
        "batch_default_vs_per_claim_c20": ratio(
            batch.get("batch-default"),
            (burst_rows.get("per-claim-c20") or {}).get("last_start_median")),
        "batch_mif50_vs_per_claim_c50": ratio(
            batch.get("batch-mif50"),
            (burst_rows.get("per-claim-c50") or {}).get("last_start_median")),
    }
    sized_ok = best_batch is not None and base and best_batch[1] <= 1.1 * base
    # None: the baseline had errors, so the rule can't be applied.
    out["ship_pr1"] = bool(sized_ok and failures_ok) if baseline_ok else None
    out["build_prefetch"] = bool(best_fill and best_batch
                                 and best_fill[1] < 0.8 * best_batch[1])
    out["prefetch_note"] = ("measured at 160 sandboxes only; the decision doc's rule asks "
                            "for 640 or more, which one kind node can't hold")
    return out


def _fmt(v, digits=2):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return str(v)


def cmd_report(args) -> int:
    burst = summarize_burst(_load("burst"))
    failures = _load("failure") + _load("long")
    v = verdict(burst, failures)
    lines = ["# claim-mode bench summary", ""]
    lines += ["## Verdict", "", "```", json.dumps(v, indent=1), "```", ""]
    lines += ["## Burst (one step, 40 prompts x 4)", "",
              "| variant | runs | errors | last start p50 (min-max) s | first group p50 s | "
              "last group p50 s | Ready p50 s | last Ready s | create window s | requests | "
              "watches | creates/s | 429s | throttled | CPU s |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in burst.items():
        lines.append(
            f"| {name} | {r['runs']} | {r['errors']} | {_fmt(r['last_start_median'])} "
            f"({_fmt(r['last_start_min'])}-{_fmt(r['last_start_max'])}) | "
            f"{_fmt(r['first_group_median'])} | {_fmt(r['last_group_median'])} | "
            f"{_fmt(r['ready_p50_median'])} | {_fmt(r['ready_last_median'])} | "
            f"{_fmt(r['create_window_median'])} | {_fmt(r['requests_median'], 0)} | {_fmt(r['watches_median'], 0)} | "
            f"{_fmt(r['creates_per_s_median'], 0)} | {r['http_429']} | "
            f"{r['throttled_logs']} | {_fmt(r['driver_cpu_median'])} |")
    lines += ["", "## Rolling", "",
              "| variant | run | rollouts/min | acquire p50/p90/p99 s | requests/rollout | "
              "watches | min warm Ready | starved | 429s | throttled | errors |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in _load("rolling"):
        if r.get("exception"):
            lines.append(f"| {r['label']} | - | exception: {r['exception'][:80]} |")
            continue
        a = r.get("acquire_s") or {}
        lines.append(
            f"| {r['variant']} | {r['run']} | {_fmt(r['rollouts_per_min'], 1)} | "
            f"{_fmt(a.get('p50'))}/{_fmt(a.get('p90'))}/{_fmt(a.get('p99'))} | "
            f"{_fmt(r['requests_per_rollout'])} | {r['watches']} | {r['min_warm_ready']} | "
            f"{r['starved']} | {r.get('http_429', 0)} | {r.get('throttled_logs', 0)} | "
            f"{r['errors']} |")
    lines += ["", "## Lease writes", "",
              "| variant | workers | rollouts | Lease writes per process per hour "
              "(measured / analytic) | requests/rollout | errors |", "|---|---|---|---|---|---|"]
    for r in _load("lease"):
        if r.get("exception"):
            lines.append(f"| {r['label']} | exception: {r['exception'][:80]} |")
            continue
        lines.append(f"| {r['variant']} | {r['workers']} | {r['rollouts']} | "
                     f"{_fmt(r['lease_writes_per_process_hour'], 0)} / "
                     f"{_fmt(r.get('lease_writes_per_process_hour_analytic'), 0)} | "
                     f"{_fmt(r['requests_per_rollout'])} | {r['errors']} |")
    lines += ["", "## Composition, failures and the long run", "",
              "| run | pass | details |", "|---|---|---|"]
    for r in _load("composition") + failures:
        keep = {k: r[k] for k in r if k not in (
            "provenance", "requests", "statuses", "worker", "label", "step", "run",
            "wall_started", "pools", "ready_per_pool", "pods_in_namespace")}
        lines.append(f"| {r['label']} | {r.get('pass')} | `{json.dumps(keep)[:400]}` |")
    lines += ["", "Not measured here: snapshot suspend/resume/restore on a batch member "
              "(needs GKE Pod Snapshots); the async fleet (sync only); connections; the "
              "cold and throttled create path (every claim is warm-covered, so create_rps "
              "only applies after a 429); scale past 160. Rolling holds are lognormal, "
              "median 15 s and p99 about 150 s, capped at 150 s, shorter than the decision "
              "doc's 60 s median so that 240 s runs hold several rollouts per env. The "
              "SIGKILL reaper is a 5 s loop in the driver, not the CronJob, so "
              "claims_gone_after_kill_s leaves out the CronJob's schedule interval.", ""]
    with open(os.path.join(RESULTS, "summary.md"), "w") as f:
        f.write("\n".join(lines))
    print(os.path.join(RESULTS, "summary.md"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--context", default=CONTEXT)
    p.add_argument("--namespace", default=NAMESPACE)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("images")
    s.add_argument("--kind-cluster", default=KIND_CLUSTER)
    sub.add_parser("setup")
    s = sub.add_parser("suite")
    s.add_argument("--only", nargs="*", default=None,
                   help="label prefixes to run, for example burst- or fail-stall")
    s.add_argument("--allow-dirty", action="store_true")
    sub.add_parser("status")
    sub.add_parser("report")
    s = sub.add_parser("worker")
    s.add_argument("--mode", choices=["lease", "hold"], required=True)
    s.add_argument("--report", required=True)
    s.add_argument("--lease", type=int, default=300)
    s.add_argument("--shape", default=ROLLING.name)
    s.add_argument("--seconds", type=float, default=180)
    s.add_argument("--n", type=int, default=20)
    s.add_argument("--hold", type=float, default=3600)
    s.add_argument("--partition-after", type=float, default=None)
    s.add_argument("--partition-for", type=float, default=60)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Worker options are parsed after the subcommand, so carry the cluster flags.
    return {"images": cmd_images, "setup": cmd_setup, "suite": cmd_suite,
            "status": cmd_status, "report": cmd_report, "worker": cmd_worker}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
