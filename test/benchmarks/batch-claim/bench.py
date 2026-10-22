#!/usr/bin/env python3
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

"""Benchmarks the Python SDK's batch claims against a kind cluster.

It changes no SDK code: the driver wraps the Kubernetes clients' ApiClient.call_api and methods
on its client's K8sHelper to count and time requests, and samples its own process with psutil.
Baselines run the single-claim API from the SDK at the commit the batch work branched from.

Subcommands:
  setup          namespaces, the base pools, and the driver pod with the README's driver Role
  import-legacy  copies the earlier runs that still count into results/, marked imported
  suite          runs every scenario's runs that have no result file yet, in run_plan() order
  candidate      applies the default-pacing rule to the 3s and 3a2 runs and writes results/candidate.json
  errored        lists runs that finished with errors; --supersede moves first attempts to results/superseded/
  report         writes bench-report.md from results/
  run            one driver run; started by suite, not by hand

Each run writes results/<scenario>-<variant>-<shell>-run<k>.json once it finishes and never
overwrites one, so a stopped suite resumes where it left off. results/STATUS.md has one line
per run, regenerated from the files on disk. Variants that share an ``interleave`` key run as
run 1 of each, then run 2 of each, so drift in the cluster spreads evenly across them. Each run is its own driver process, so its RSS and CPU numbers start clean, and the
driver stops at phase boundaries while the orchestrator scrapes the apiserver's /metrics.
"""

import argparse
import asyncio
import functools
import glob
import inspect
import json
import logging
import os
import platform
import resource
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

NS = "batch-bench"
DRIVER_NS = "batch-bench-driver"
DRIVER_POD = "bench-driver"
UNSCHED_POOL = "unsched-pool"
COLD_POOL = "ready-pool"
WARM_POOL = "warm-pool"
POD_CIDR_PREFIX = "10.244."
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
RESULTS_DIR = os.environ.get("BENCH_RESULTS", os.path.join(HERE, "results"))
LOGS_DIR = os.path.join(RESULTS_DIR, "logs")
# Where the kind node mounts the repo (see kind-config.yaml).
POD_REPO_ROOT = "/src"
IMAGE = "kind.local/sandboxd:bench"
TIP_BRANCH = "feat/batch-reaper-ymtd6t"
# The code under test, by commit and tree. The tip branch can be rebased onto an identical tree,
# so each result records the commit it means and the branch's commit at run time apart.
CODE_SHA = "44fb487f361e3d60eb4690fafc6db9fa04d4fa92"
CODE_TREE = "8a7adc54a27eb8291289c6fb166c34c69ea47143"
BASE_SHA = "7fd162d9d5c33fd0c8af9fbb15c6453ccf5ddd58"
BASE_TREE = "588a31efbc306a7892c88f1ffd9e6c575019ad10"
# Upstream main and the fix branch on it, for the 6v2-leak runs.
LEAK_BASE_SHA = "840fdfde30129480050d025017651fafa70b33c2"
LEAK_FIX_SHA = "2e02e42078d364917e80722ab8f4aaff8da8bcfe"
# Tiny sizes and one run per variant, for checking that every metric is recorded.
SMOKE = os.environ.get("BENCH_SMOKE") == "1"
PYTHONS = {
    ("host", "tip"): os.path.join(REPO, "bin", "bench-venv", "bin", "python"),
    ("host", "base"): os.path.join(REPO, "bin", "base-venv", "bin", "python"),
    ("pod", "tip"): "/opt/venv/bin/python",
    ("pod", "base"): "/opt/venv-base/bin/python",
}
EXTRA_LABEL = "bench/extra"


def template_manifest(name, shape, extra=False):
    labels = f"\n  labels:\n    {EXTRA_LABEL}: \"true\"" if extra else ""
    if shape == "unsched":
        pod = f"""      nodeSelector:
        bench/never: "true"
      containers:
      - name: sandboxd
        image: {IMAGE}"""
    else:
        pod = f"""      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        fsGroup: 1000
      containers:
      - name: sandboxd
        image: {IMAGE}
        imagePullPolicy: IfNotPresent
        securityContext:
          runAsUser: 1000
          runAsGroup: 1000
          runAsNonRoot: true
          allowPrivilegeEscalation: false
          capabilities:
            drop: [ALL]
        resources:
          requests:
            cpu: 5m
            memory: 16Mi
          limits:
            memory: 128Mi
        ports:
        - containerPort: 8080
        - containerPort: 9090
        readinessProbe:
          httpGet:
            path: /v1/health
            port: 8080
          periodSeconds: 1
          failureThreshold: 30
          timeoutSeconds: 2"""
    # Unmanaged: the default policy admits only the router, and the driver talks to pod IPs.
    return f"""apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: {name}
  namespace: {NS}{labels}
spec:
  networkPolicyManagement: Unmanaged
  podTemplate:
    spec:
{pod}
"""


def pool_manifest(name, template, replicas, extra=False):
    labels = f"\n  labels:\n    {EXTRA_LABEL}: \"true\"" if extra else ""
    return f"""apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxWarmPool
metadata:
  name: {name}
  namespace: {NS}{labels}
spec:
  replicas: {replicas}
  sandboxTemplateRef:
    name: {template}
"""


# The README's batch driver Role, plus get on Sandboxes, which the pod-IP connection mode and the
# baseline's get_pod_name() and get_pod_ip() read.
SETUP_MANIFEST = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {NS}
---
{template_manifest("unsched-template", "unsched")}---
{pool_manifest(UNSCHED_POOL, "unsched-template", 0)}---
{template_manifest("ready-template", "ready")}---
{pool_manifest(COLD_POOL, "ready-template", 0)}---
apiVersion: v1
kind: Namespace
metadata:
  name: {DRIVER_NS}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: bench-driver
  namespace: {DRIVER_NS}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: sandbox-batch-driver
  namespace: {NS}
rules:
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxclaims"]
  verbs: ["create", "get", "list", "watch", "delete", "deletecollection"]
- apiGroups: ["extensions.agents.x-k8s.io"]
  resources: ["sandboxwarmpools", "sandboxtemplates"]
  verbs: ["get"]
- apiGroups: ["coordination.k8s.io"]
  resources: ["leases"]
  verbs: ["create", "get", "update", "delete"]
- apiGroups: ["agents.x-k8s.io"]
  resources: ["sandboxes"]
  verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: bench-driver
  namespace: {NS}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: sandbox-batch-driver
subjects:
- kind: ServiceAccount
  name: bench-driver
  namespace: {DRIVER_NS}
---
apiVersion: v1
kind: Pod
metadata:
  name: {DRIVER_POD}
  namespace: {DRIVER_NS}
spec:
  serviceAccountName: bench-driver
  containers:
  - name: driver
    image: python:3.12-slim
    command: ["sleep", "infinity"]
    volumeMounts:
    - name: src
      mountPath: {POD_REPO_ROOT}
      readOnly: true
  volumes:
  - name: src
    hostPath:
      path: {POD_REPO_ROOT}
"""


# ---------------------------------------------------------------------------
# Driver side
# ---------------------------------------------------------------------------


def _status_of(error):
    status = getattr(error, "status", None)
    return status if isinstance(status, int) else -1


def _percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _dist(values, scale=1.0):
    values = [v * scale for v in values]
    if not values:
        return None
    return {"n": len(values), "first": min(values), "p50": _percentile(values, 50),
            "p90": _percentile(values, 90), "p99": _percentile(values, 99), "last": max(values)}


def classify_request(method, path, query):
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
    resource_ = rest[0] if rest else "?"
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
    return f"{verb} {resource_}{'/' + sub if sub else ''}"


class RequestCounter:
    """Counts every request either Kubernetes client sends, by verb and resource."""

    def __init__(self):
        self.lock = threading.Lock()
        self.counts = {}

    def add(self, key):
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1

    def snapshot(self):
        with self.lock:
            return dict(self.counts)

    @staticmethod
    def since(now, before):
        return {k: v - before.get(k, 0) for k, v in now.items() if v - before.get(k, 0)}

    def install(self):
        counter = self
        try:
            from kubernetes.client import ApiClient as SyncApiClient

            orig_sync = SyncApiClient.call_api

            def call_api(self, method, url, *args, **kwargs):
                parsed = urlparse(url)
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                counter.add(classify_request(method, parsed.path, query))
                return orig_sync(self, method, url, *args, **kwargs)

            SyncApiClient.call_api = call_api
        except ImportError:
            pass
        try:
            from kubernetes_asyncio.client import ApiClient as AsyncApiClient

            orig_async = AsyncApiClient.call_api

            def call_api_async(self, resource_path, method, path_params=None, query_params=None, *args, **kwargs):
                path = resource_path
                for k, v in (path_params or {}).items():
                    path = path.replace("{" + k + "}", str(v))
                counter.add(classify_request(method, path, dict(query_params or [])))
                return orig_async(self, resource_path, method, path_params, query_params, *args, **kwargs)

            AsyncApiClient.call_api = call_api_async
        except ImportError:
            pass


class Probe:
    """Times the batch's requests by wrapping methods on one K8sHelper instance."""

    def __init__(self):
        self.lock = threading.Lock()
        # (claim name, start, end, status), status 0 on success.
        self.creates = []
        self.create_in_flight = 0
        self.create_peak_in_flight = 0
        # (start, end) of successful Lease creates and replaces.
        self.lease_ok = []
        self.lease_failures = 0
        # (start, end, item count)
        self.lists = []
        # (start, end, status)
        self.deletecollections = []
        self.watches_active = 0
        self.watches_peak = 0

    def install(self, helper):
        self._wrap(helper, "create_sandbox_claim", self._on_create, self._before_create)
        self._wrap(helper, "create_batch_lease", self._on_lease)
        self._wrap(helper, "replace_batch_lease", self._on_lease)
        self._wrap(helper, "list_sandbox_claim_objects", self._on_list)
        self._wrap(helper, "delete_sandbox_claims_by_label", self._on_deletecollection)
        # The batch's claim watch, and the single-claim API's per-claim wait.
        self._wrap_watch(helper, "watch_sandbox_claims")
        self._wrap_watch(helper, "_watch_claim")

    def _wrap(self, helper, name, on_done, before=None):
        fn = getattr(helper, name, None)
        if fn is None:
            return
        if inspect.iscoroutinefunction(fn):
            async def wrapper(*args, **kwargs):
                if before:
                    before()
                start = time.monotonic()
                try:
                    result = await fn(*args, **kwargs)
                except BaseException as e:
                    on_done(args, start, time.monotonic(), _status_of(e), None)
                    raise
                on_done(args, start, time.monotonic(), 0, result)
                return result
        else:
            def wrapper(*args, **kwargs):
                if before:
                    before()
                start = time.monotonic()
                try:
                    result = fn(*args, **kwargs)
                except BaseException as e:
                    on_done(args, start, time.monotonic(), _status_of(e), None)
                    raise
                on_done(args, start, time.monotonic(), 0, result)
                return result
        setattr(helper, name, wrapper)

    def _wrap_watch(self, helper, name):
        fn = getattr(helper, name, None)
        if fn is None:
            return
        if inspect.isasyncgenfunction(fn):
            async def wrapper(*args, **kwargs):
                self._watch(1)
                try:
                    async for event in fn(*args, **kwargs):
                        yield event
                finally:
                    self._watch(-1)
        elif inspect.iscoroutinefunction(fn):
            async def wrapper(*args, **kwargs):
                self._watch(1)
                try:
                    return await fn(*args, **kwargs)
                finally:
                    self._watch(-1)
        elif inspect.isgeneratorfunction(fn):
            def wrapper(*args, **kwargs):
                self._watch(1)
                try:
                    yield from fn(*args, **kwargs)
                finally:
                    self._watch(-1)
        else:
            def wrapper(*args, **kwargs):
                self._watch(1)
                try:
                    return fn(*args, **kwargs)
                finally:
                    self._watch(-1)
        setattr(helper, name, wrapper)

    def _watch(self, delta):
        with self.lock:
            self.watches_active += delta
            self.watches_peak = max(self.watches_peak, self.watches_active)

    def _before_create(self):
        with self.lock:
            self.create_in_flight += 1
            self.create_peak_in_flight = max(self.create_peak_in_flight, self.create_in_flight)

    def _on_create(self, args, start, end, status, _result):
        with self.lock:
            self.create_in_flight -= 1
            self.creates.append((args[0], start, end, status))

    def _on_lease(self, _args, start, end, status, _result):
        with self.lock:
            if status == 0:
                self.lease_ok.append((start, end))
            else:
                self.lease_failures += 1

    def _on_list(self, _args, start, end, status, result):
        items = len(result[0]) if status == 0 and result else 0
        with self.lock:
            self.lists.append((start, end, items))

    def _on_deletecollection(self, _args, start, end, status, _result):
        with self.lock:
            self.deletecollections.append((start, end, status))

    def created_names(self):
        with self.lock:
            # A 409 on a retry means an earlier attempt landed.
            return {c[0] for c in self.creates if c[3] == 0} | {
                c[0] for c in self.creates if c[3] == 409 and any(d[0] == c[0] and d[3] != 409 for d in self.creates)
            }


class ReadyRecorder:
    """Records when the driver first sees each member Ready, by wrapping the batch's watch upsert."""

    def __init__(self):
        self.lock = threading.Lock()
        self.first_ready = {}

    def mark(self, name):
        with self.lock:
            self.first_ready.setdefault(name, time.monotonic())

    def install(self):
        try:
            from k8s_agent_sandbox import batch_state
            from k8s_agent_sandbox.models import MemberState
        except ImportError:
            return
        orig = batch_state.BatchState.upsert_claim
        recorder = self

        def upsert_claim(self, claim_obj):
            member = orig(self, claim_obj)
            if member is not None and member.state is MemberState.READY:
                recorder.mark(member.claim_name)
            return member

        batch_state.BatchState.upsert_claim = upsert_claim

    def stats(self, t0):
        with self.lock:
            return _dist([t - t0 for t in self.first_ready.values()])


class Sampler(threading.Thread):
    """Samples this process's RSS, TCP connections and the batch's lease_degraded()."""

    def __init__(self, api_endpoints, interval=0.1):
        super().__init__(daemon=True)
        import psutil

        self.proc = psutil.Process()
        self.api_endpoints = api_endpoints
        self.interval = interval
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.rss_peak = 0
        self.window_peak = 0
        self.api_conns_peak = 0
        self.pod_conns_peak = 0
        self.pod_tuples = set()
        self.track_pod_tuples = False
        self.batch = None
        self.degraded_ever = False
        self.rss_series = []
        # glibc's mallinfo2() next to each RSS sample, when a run asks for the malloc probe.
        self.malloc_series = None
        self.started = time.monotonic()
        # CPU this thread spends, subtracted from the driver's CPU.
        self.own_cpu = 0.0

    def run(self):
        next_series = 0.0
        while not self.stop.is_set():
            t = time.thread_time()
            self.sample()
            now = time.monotonic() - self.started
            if now >= next_series:
                self.rss_series.append((round(now, 1), self.proc.memory_info().rss / 2**20))
                if self.malloc_series is not None:
                    self.malloc_series.append((round(now, 1), _mallinfo2()))
                next_series += 10
            self.own_cpu += time.thread_time() - t
            self.stop.wait(self.interval)

    def sample(self):
        rss = self.proc.memory_info().rss
        api = pod = 0
        tuples = []
        for c in self.proc.net_connections(kind="tcp"):
            if not c.raddr:
                continue
            # gRPC dials over dual-stack sockets, which report IPv4 peers as ::ffff:a.b.c.d.
            ip = c.raddr.ip.removeprefix("::ffff:")
            if (ip, c.raddr.port) in self.api_endpoints:
                if c.status == "ESTABLISHED":
                    api += 1
            elif ip.startswith(POD_CIDR_PREFIX):
                pod += 1
                tuples.append((c.laddr.port, ip, c.raddr.port))
        batch = self.batch
        degraded = batch is not None and batch.lease_degraded()
        with self.lock:
            self.rss_peak = max(self.rss_peak, rss)
            self.window_peak = max(self.window_peak, rss)
            self.api_conns_peak = max(self.api_conns_peak, api)
            self.pod_conns_peak = max(self.pod_conns_peak, pod)
            if self.track_pod_tuples:
                self.pod_tuples.update(tuples)
            self.degraded_ever = self.degraded_ever or degraded

    def window(self):
        """Starts a new RSS window and returns the current RSS."""
        rss = self.proc.memory_info().rss
        with self.lock:
            self.window_peak = rss
        return rss

    def window_delta(self, start_rss):
        self.sample()
        with self.lock:
            return self.window_peak - start_rss


_MALLINFO2_FIELDS = ("arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")


@functools.lru_cache(maxsize=None)
def _libc():
    """libc with mallinfo2's return type set, once, so sampling doesn't allocate new ctypes types."""
    import ctypes

    try:
        libc = ctypes.CDLL("libc.so.6")
    except OSError:
        return None
    if hasattr(libc, "mallinfo2"):
        class Mallinfo2(ctypes.Structure):
            _fields_ = [(name, ctypes.c_size_t) for name in _MALLINFO2_FIELDS]

        libc.mallinfo2.restype = Mallinfo2
    return libc


def _mallinfo2():
    """glibc's mallinfo2() over all arenas, in bytes, or None off glibc 2.33+. uordblks is what
    malloc has handed out and not had back, fordblks what it holds free, hblkhd its mmapped chunks.
    pymalloc's arenas are mmapped by CPython itself, so they are in RSS but in none of these."""
    libc = _libc()
    if libc is None or not hasattr(libc, "mallinfo2"):
        return None
    info = libc.mallinfo2()
    return {name: getattr(info, name) for name in _MALLINFO2_FIELDS}


class DiscardCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0
        self.first = None

    def emit(self, record):
        message = record.getMessage()
        if "Connection pool is full, discarding connection" in message:
            self.count += 1
            self.first = self.first or message


def _tcp_active_opens():
    """The netns's TCP ActiveOpens counter, or None off Linux."""
    try:
        with open("/proc/net/snmp") as f:
            lines = [line.split() for line in f if line.startswith("Tcp:")]
        return int(lines[1][lines[0].index("ActiveOpens")])
    except OSError:
        return None


def _api_endpoints():
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return {(os.environ["KUBERNETES_SERVICE_HOST"], int(os.environ.get("KUBERNETES_SERVICE_PORT", "443")))}
    import yaml

    with open(os.environ["KUBECONFIG"]) as f:
        server = yaml.safe_load(f)["clusters"][0]["cluster"]["server"]
    url = urlparse(server)
    return {(url.hostname, url.port), ("::1" if url.hostname == "127.0.0.1" else url.hostname, url.port)}


class Driver:
    def __init__(self, spec):
        self.spec = spec
        self.is_async = spec["shell"] == "async"
        # WARNING, as A10 asks: the single-claim API logs two INFO lines per claim.
        logging.basicConfig(level=logging.WARNING)
        self.requests = RequestCounter()
        self.requests.install()
        self.ready = ReadyRecorder()
        self.ready.install()
        self.probe = Probe()
        self.sampler = Sampler(_api_endpoints())
        if spec.get("malloc_probe"):
            self.sampler.malloc_series = []
        self.discards = DiscardCounter()
        urllib3_logger = logging.getLogger("urllib3.connectionpool")
        urllib3_logger.addHandler(self.discards)
        urllib3_logger.setLevel(logging.WARNING)
        import psutil

        self.proc = psutil.Process()
        self.client = None
        self.result = {}

    # Phases: the orchestrator scrapes /metrics (or starts a cleanup tracker), then acks on stdin.
    def phase(self, name):
        print(f"@@PHASE {name}", flush=True)
        sys.stdin.readline()

    async def aphase(self, name):
        # In a thread, so the batch's renewal and watch tasks keep running while it waits.
        await asyncio.to_thread(self.phase, name)

    def cleanup_start(self, tag, mode, selector=""):
        """Tells the orchestrator a release starts now, so it times how long the run's claims
        (and, in ``pods`` mode, pods) take to go."""
        print(f"@@CLEANUP {tag} {mode} {selector}".rstrip(), flush=True)
        sys.stdin.readline()

    async def acleanup_start(self, *args):
        await asyncio.to_thread(self.cleanup_start, *args)

    def cpu(self):
        t = self.proc.cpu_times()
        return t.user + t.system - self.sampler.own_cpu

    def connection_config(self):
        from k8s_agent_sandbox.models import SandboxdInClusterConnectionConfig

        # Only the data-plane and rolling runs connect; the others never do.
        return SandboxdInClusterConnectionConfig(mode="in-cluster-pod-ip")

    def make_client(self):
        """A batch client: pool_size, when the spec sets one, else the client's default pool."""
        pool_size = self.spec.get("pool_size")
        kwargs = {} if pool_size is None else {"pool_size": pool_size}
        if self.is_async:
            from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient

            client = AsyncSandboxClient(connection_config=self.connection_config(), **kwargs)
        else:
            from k8s_agent_sandbox import SandboxClient

            client = SandboxClient(connection_config=self.connection_config(), **kwargs)
        self.probe.install(client.k8s_helper)
        self.client = client
        return client

    def make_baseline_client(self):
        """A single-claim client as agent-sandbox-rl builds it: an injected ApiClient with a
        1,000-connection pool (cluster.py build_api_client), or the default ApiClient."""
        from k8s_agent_sandbox import SandboxClient

        kwargs = {}
        if self.spec.get("baseline_pool") is not None:
            from kubernetes import client as k8s, config

            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
            configuration = k8s.Configuration.get_default_copy()
            configuration.connection_pool_maxsize = self.spec["baseline_pool"]
            kwargs["api_client"] = k8s.ApiClient(configuration=configuration)
        client = SandboxClient(connection_config=self.connection_config(), **kwargs)
        self.probe.install(client.k8s_helper)
        self.client = client
        return client

    def batch_kwargs(self):
        return {k: self.spec[k] for k in ("create_rps", "max_in_flight", "lease_duration") if self.spec.get(k) is not None}

    def record_settings(self):
        r = self.result
        import k8s_agent_sandbox

        r["sdk_file"] = k8s_agent_sandbox.__file__
        try:
            from importlib.metadata import version

            r["sdk_version"] = version("k8s-agent-sandbox")
            r["kubernetes_version"] = version("kubernetes")
            r["kubernetes_asyncio_version"] = version("kubernetes_asyncio")
        except Exception as e:
            r["sdk_version"] = repr(e)
        r["python"] = sys.executable
        r["python_version"] = platform.python_version()
        r["platform"] = platform.platform()
        # From inside the driver, so the result shows what the process actually ran with.
        r["malloc_arena_max"] = os.environ.get("MALLOC_ARENA_MAX")
        r["libc"] = platform.libc_ver()
        settings = {k: self.spec.get(k) for k in ("create_rps", "max_in_flight", "pool_size", "lease_duration",
                                                   "quorum_timeout", "work_budget", "concurrency", "baseline_pool")}
        try:
            from k8s_agent_sandbox import batch_utils as bu

            # What the SDK used where the spec left a setting at its default.
            defaults = {"create_rps": bu.BATCH_DEFAULT_CREATE_RPS, "max_in_flight": bu.BATCH_DEFAULT_MAX_IN_FLIGHT,
                        "lease_duration": bu.BATCH_DEFAULT_LEASE_DURATION_SECONDS,
                        "quorum_timeout": bu.BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS,
                        "work_budget": bu.BATCH_DEFAULT_WORK_BUDGET_SECONDS}
            if self.spec["kind"] not in ("baseline", "baseline_dataplane", "baseline_rolling"):
                for k, v in defaults.items():
                    if settings.get(k) is None:
                        settings[k] = v
        except ImportError:
            pass
        r["settings"] = settings
        helper = getattr(self.client, "k8s_helper", None)
        api = getattr(helper, "custom_objects_api", None)
        r["effective_pool_size"] = api.api_client.configuration.connection_pool_maxsize if api else None

    def finish(self):
        self.sampler.stop.set()
        self.sampler.join()
        p = self.probe
        r = self.result
        self.record_settings()
        r["rss_peak_mb"] = self.sampler.rss_peak / 2**20
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        r["ru_maxrss_mb"] = maxrss / 2**20 if sys.platform == "darwin" else maxrss / 1024
        r["rss_series_mb"] = self.sampler.rss_series
        if self.sampler.malloc_series is not None:
            r["mallinfo2_series"] = self.sampler.malloc_series
        r["api_conns_peak"] = self.sampler.api_conns_peak
        r["pool_discard_warnings"] = self.discards.count
        r["pool_discard_first"] = self.discards.first
        r["lease_degraded_ever"] = self.sampler.degraded_ever
        r["lease_failures"] = p.lease_failures
        r["watches_peak_sdk"] = p.watches_peak
        r["requests_total"] = self.requests.snapshot()
        r["sampler_cpu_s"] = self.sampler.own_cpu
        print("@@RESULT " + json.dumps(r), flush=True)

    def fill_stats(self, t0, n):
        p = self.probe
        with p.lock:
            creates = list(p.creates)
        latencies = [(c[2] - c[1]) * 1000 for c in creates]
        attempts = {}
        for c in creates:
            attempts[c[0]] = attempts.get(c[0], 0) + 1
        r = self.result
        r["create_attempts"] = len(creates)
        r["creates_ok"] = len(p.created_names())
        r["creates_retried"] = sum(1 for k in attempts.values() if k > 1)
        r["create_429s"] = sum(1 for c in creates if c[3] == 429)
        r["create_error_statuses"] = sorted({c[3] for c in creates if c[3] != 0})
        r["create_p50_ms"] = _percentile(latencies, 50)
        r["create_p99_ms"] = _percentile(latencies, 99)
        r["create_max_ms"] = max(latencies) if latencies else None
        r["create_peak_in_flight"] = p.create_peak_in_flight
        if creates:
            r["fill_sent_s"] = max(c[1] for c in creates) - t0
            r["fill_acked_s"] = max(c[2] for c in creates) - t0
            r["creates_per_s"] = n / r["fill_acked_s"]

    def adoption_stats(self, ready_at, t0):
        """Each member's Ready time minus its create's ack, joined by claim name, and the tail from
        the last create sent to the last Ready. ``ready_at`` maps claim names to monotonic times."""
        r = self.result
        with self.probe.lock:
            acked = {c[0]: c[2] for c in self.probe.creates if c[3] == 0}
        lat = [ready_at[name] - acked[name] for name in ready_at if name in acked]
        r["adoption_s"] = _dist(lat)
        r["adoption_joined"] = len(lat)
        if ready_at and r.get("fill_sent_s") is not None:
            r["tail_after_fill_s"] = max(ready_at.values()) - t0 - r["fill_sent_s"]

    def lease_stats(self, start, end):
        """Renewals between ``start`` and ``end``: count, each one's latency, and the longest gap."""
        writes = sorted((s, e) for s, e in self.probe.lease_ok if start <= e <= end)
        ends = [e for _, e in writes]
        gaps = [b - a for a, b in zip(ends, ends[1:])]
        r = self.result
        r["lease_writes"] = len(writes)
        r["renewal_latency_ms"] = _dist([e - s for s, e in writes[1:]], 1000)
        r["renewal_gap_max_s"] = max(gaps) if gaps else None
        r["renew_interval_s"] = max(1, (self.spec.get("lease_duration") or 60) // 3)

    def wait_until(self, cond, batch, timeout, what):
        deadline = time.monotonic() + timeout
        while not cond():
            if batch is not None and batch.err() is not None:
                raise RuntimeError(f"batch stopped during {what}: {batch.err()!r}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} did not finish in {timeout}s")
            time.sleep(0.05)

    async def await_until(self, cond, batch, timeout, what):
        deadline = time.monotonic() + timeout
        while not cond():
            if batch is not None and batch.err() is not None:
                raise RuntimeError(f"batch stopped during {what}: {batch.err()!r}")
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} did not finish in {timeout}s")
            await asyncio.sleep(0.05)

    def record_release(self, mark_lists, mark_dc, start, end, rss_delta, prefix="release"):
        p = self.probe
        lists = p.lists[mark_lists:]
        dcs = p.deletecollections[mark_dc:]
        r = self.result
        wall = end - start
        r[f"{prefix}_s"] = wall
        r[f"{prefix}_rounds"] = len(dcs)
        r[f"{prefix}_deletecollection_s"] = sum(d[1] - d[0] for d in dcs)
        r[f"{prefix}_list_s"] = sum(e - s for s, e, _ in lists)
        r[f"{prefix}_list_max_s"] = max((e - s for s, e, _ in lists), default=None)
        r[f"{prefix}_list_fraction"] = r[f"{prefix}_list_s"] / wall if wall else None
        r[f"{prefix}_deletecollection_fraction"] = r[f"{prefix}_deletecollection_s"] / wall if wall else None
        r[f"{prefix}_rss_delta_mb"] = rss_delta / 2**20

    @staticmethod
    def groups_of(spec):
        from k8s_agent_sandbox.models import BatchGroup

        return [BatchGroup(warmpool=g[0], size=g[1], min_ready=g[2] if len(g) > 2 else None) for g in spec["groups"]]

    # ---- scenarios ----

    def run(self):
        kind = self.spec["kind"]
        if self.spec.get("no_pool_check"):
            # 2c's control only: the pool-size check off, here and nowhere else.
            from k8s_agent_sandbox import batch_utils

            batch_utils.check_pool_size = lambda *args, **kwargs: None
        self.sampler.start()
        try:
            if self.is_async:
                asyncio.run(getattr(self, f"a_{kind}")())
            else:
                getattr(self, f"s_{kind}")()
        finally:
            self.finish()

    def s_poolcheck(self):
        client = self.make_client()
        r = self.result
        r["default_pool_size"] = client.k8s_helper.api_pool_size()
        try:
            batch = client.claim_batch(self.groups_of(self.spec), namespace=NS, **self.batch_kwargs())
        except ValueError as e:
            r["raised"] = f"ValueError: {e}"
            return
        r["raised"] = None
        batch.release()

    async def a_poolcheck(self):
        client = self.make_client()
        r = self.result
        r["default_pool_size"] = await client.k8s_helper.api_pool_size()
        try:
            batch = await client.claim_batch(self.groups_of(self.spec), namespace=NS, **self.batch_kwargs())
        except ValueError as e:
            r["raised"] = f"ValueError: {e}"
            await client.close()
            return
        r["raised"] = None
        await batch.release()
        await client.close()

    def s_fill(self):
        spec, r = self.spec, self.result
        n = sum(g[1] for g in spec["groups"])
        client = self.make_client()
        client.k8s_helper.api_pool_size()  # loads the config before the timed section
        self.phase("start")
        req0 = self.requests.snapshot()
        cpu0 = self.cpu()
        t0 = time.monotonic()
        batch = client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        r["claim_batch_return_s"] = time.monotonic() - t0
        r["requests_claim_batch_call"] = self.requests.since(self.requests.snapshot(), req0)
        self.sampler.batch = batch
        rps = spec.get("create_rps") or 50
        self.wait_until(lambda: len(self.probe.created_names()) >= n, batch, n / rps * 3 + 300, "the fill")
        fill_end = time.monotonic()
        r["fill_cpu_s"] = self.cpu() - cpu0
        r["fill_wall_s"] = fill_end - t0
        r["fill_cpu_cores"] = r["fill_cpu_s"] / r["fill_wall_s"]
        self.fill_stats(t0, n)
        r["requests_fill"] = self.requests.since(self.requests.snapshot(), req0)
        # Held long enough for at least two renewals.
        while time.monotonic() < t0 + spec.get("hold_min_s", 45):
            time.sleep(0.2)
        r["batch_err"] = repr(batch.err()) if batch.err() else None
        self.phase("filled")
        t = time.perf_counter()
        members = batch.members()
        r["members_ms"] = (time.perf_counter() - t) * 1000
        r["members_count"] = len(members)
        if spec.get("after") == "groups":
            pool = spec["groups"][0][0]
            t = time.perf_counter()
            batch.members(pool)
            r["members_pool_ms"] = (time.perf_counter() - t) * 1000
            self.lease_stats(t0 - 1, time.monotonic())
            from k8s_agent_sandbox import batch_utils

            mark = (len(self.probe.lists), len(self.probe.deletecollections))
            self.cleanup_start("release_group", "count", batch_utils.group_label_selector(batch.batch_id, pool))
            rss0 = self.sampler.window()
            t = time.monotonic()
            batch.release_group(pool)
            self.record_release(*mark, t, time.monotonic(), self.sampler.window_delta(rss0), "release_group")
        elif spec.get("after") == "release":
            self.lease_stats(t0 - 1, time.monotonic())
        elif spec.get("after") == "memprobe":
            self.lease_stats(t0 - 1, time.monotonic())
            self.memprobe(client, batch)
            return
        else:
            t = time.monotonic()
            batch.detach()
            r["detach_s"] = time.monotonic() - t
            self.sampler.batch = None
            # Ends before detach's own Lease write, which isn't a renewal.
            self.lease_stats(t0 - 1, t)
            mark = len(self.probe.lists)
            rss0 = self.sampler.window()
            t = time.monotonic()
            batch = client.get_batch(batch.batch_id, namespace=NS)
            r["attach_s"] = time.monotonic() - t
            r["attach_rss_delta_mb"] = self.sampler.window_delta(rss0) / 2**20
            r["attach_list_s"] = sum(e - s for s, e, _ in self.probe.lists[mark:])
        mark = (len(self.probe.lists), len(self.probe.deletecollections))
        self.cleanup_start("release", "count")
        rss0 = self.sampler.window()
        t = time.monotonic()
        batch.release()
        self.record_release(*mark, t, time.monotonic(), self.sampler.window_delta(rss0))
        self.phase("released")

    def memprobe(self, client, batch):
        """tracemalloc's peak and retained sizes for one direct list of the batch's claims, and
        then for release(). Traced sizes are relative to the traced size just before each call."""
        import gc
        import tracemalloc

        from k8s_agent_sandbox import batch_utils

        r = self.result
        # The selector the batch's own list, watch and release use (sandbox_batch.py).
        selector = f"{batch_utils.BATCH_ID_LABEL}={batch.batch_id}"
        r["memprobe_selector"] = selector
        mb = 2**20
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        t = time.monotonic()
        items, _ = client.k8s_helper.list_sandbox_claim_objects(NS, selector)
        r["list_s"] = time.monotonic() - t
        r["list_items"] = len(items)
        current, peak = tracemalloc.get_traced_memory()
        r["list_traced_peak_mb"] = (peak - before) / mb
        r["list_traced_with_result_mb"] = (current - before) / mb
        del items
        gc.collect()
        r["list_traced_retained_mb"] = (tracemalloc.get_traced_memory()[0] - before) / mb
        tracemalloc.reset_peak()
        before = tracemalloc.get_traced_memory()[0]
        mark = (len(self.probe.lists), len(self.probe.deletecollections))
        self.cleanup_start("release", "count")
        rss0 = self.sampler.window()
        t = time.monotonic()
        batch.release()
        self.record_release(*mark, t, time.monotonic(), self.sampler.window_delta(rss0))
        peak = tracemalloc.get_traced_memory()[1]
        gc.collect()
        r["release_traced_peak_mb"] = (peak - before) / mb
        r["release_traced_retained_mb"] = (tracemalloc.get_traced_memory()[0] - before) / mb
        tracemalloc.stop()
        self.phase("released")

    async def a_fill(self):
        spec, r = self.spec, self.result
        n = sum(g[1] for g in spec["groups"])
        client = self.make_client()
        await client.k8s_helper.api_pool_size()
        await self.aphase("start")
        req0 = self.requests.snapshot()
        cpu0 = self.cpu()
        t0 = time.monotonic()
        batch = await client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        r["claim_batch_return_s"] = time.monotonic() - t0
        r["requests_claim_batch_call"] = self.requests.since(self.requests.snapshot(), req0)
        self.sampler.batch = batch
        rps = spec.get("create_rps") or 50
        await self.await_until(lambda: len(self.probe.created_names()) >= n, batch, n / rps * 3 + 300, "the fill")
        fill_end = time.monotonic()
        r["fill_cpu_s"] = self.cpu() - cpu0
        r["fill_wall_s"] = fill_end - t0
        r["fill_cpu_cores"] = r["fill_cpu_s"] / r["fill_wall_s"]
        self.fill_stats(t0, n)
        r["requests_fill"] = self.requests.since(self.requests.snapshot(), req0)
        while time.monotonic() < t0 + spec.get("hold_min_s", 45):
            await asyncio.sleep(0.2)
        r["batch_err"] = repr(batch.err()) if batch.err() else None
        await self.aphase("filled")
        t = time.perf_counter()
        members = batch.members()
        r["members_ms"] = (time.perf_counter() - t) * 1000
        r["members_count"] = len(members)
        if spec.get("after") == "release":
            self.lease_stats(t0 - 1, time.monotonic())
        else:
            t = time.monotonic()
            await batch.detach()
            r["detach_s"] = time.monotonic() - t
            self.sampler.batch = None
            self.lease_stats(t0 - 1, t)
            mark = len(self.probe.lists)
            rss0 = self.sampler.window()
            t = time.monotonic()
            batch = await client.get_batch(batch.batch_id, namespace=NS)
            r["attach_s"] = time.monotonic() - t
            r["attach_rss_delta_mb"] = self.sampler.window_delta(rss0) / 2**20
            r["attach_list_s"] = sum(e - s for s, e, _ in self.probe.lists[mark:])
        mark = (len(self.probe.lists), len(self.probe.deletecollections))
        await self.acleanup_start("release", "count")
        rss0 = self.sampler.window()
        t = time.monotonic()
        await batch.release()
        self.record_release(*mark, t, time.monotonic(), self.sampler.window_delta(rss0))
        await client.close()
        await self.aphase("released")

    def s_ready(self):
        spec, r = self.spec, self.result
        n = sum(g[1] for g in spec["groups"])
        client = self.make_client()
        client.k8s_helper.api_pool_size()
        self.phase("start")
        req0 = self.requests.snapshot()
        cpu0 = self.cpu()
        t0 = time.monotonic()
        batch = client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        self.sampler.batch = batch
        if spec.get("consumer") == "groups":
            group_times, errors = [], []
            for group in batch.iter_ready_groups():
                group_times.append(time.monotonic() - t0)
                if group.error is not None:
                    errors.append(repr(group.error))
            r["groups_ready"] = _dist(group_times)
            r["group_errors"] = errors[:5]
            r["group_error_count"] = len(errors)
        else:
            quorum = batch.wait_for_quorum()
            r["quorum_count"] = len(quorum)
        r["all_ready_s"] = time.monotonic() - t0
        r["ready_cpu_s"] = self.cpu() - cpu0
        r["member_ready_s"] = self.ready.stats(t0)
        self.fill_stats(t0, n)
        self.adoption_stats(dict(self.ready.first_ready), t0)
        self.lease_stats(t0 - 1, time.monotonic())
        r["requests_until_ready"] = self.requests.since(self.requests.snapshot(), req0)
        self.phase("ready")
        self.cleanup_start("release", "pods")
        t = time.monotonic()
        batch.release()
        r["release_s"] = time.monotonic() - t
        self.phase("released")

    async def a_ready(self):
        spec, r = self.spec, self.result
        n = sum(g[1] for g in spec["groups"])
        client = self.make_client()
        await client.k8s_helper.api_pool_size()
        await self.aphase("start")
        req0 = self.requests.snapshot()
        cpu0 = self.cpu()
        t0 = time.monotonic()
        batch = await client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        self.sampler.batch = batch
        quorum = await batch.wait_for_quorum()
        r["quorum_count"] = len(quorum)
        r["all_ready_s"] = time.monotonic() - t0
        r["ready_cpu_s"] = self.cpu() - cpu0
        r["member_ready_s"] = self.ready.stats(t0)
        self.fill_stats(t0, n)
        self.adoption_stats(dict(self.ready.first_ready), t0)
        self.lease_stats(t0 - 1, time.monotonic())
        r["requests_until_ready"] = self.requests.since(self.requests.snapshot(), req0)
        await self.aphase("ready")
        await self.acleanup_start("release", "pods")
        t = time.monotonic()
        await batch.release()
        r["release_s"] = time.monotonic() - t
        await client.close()
        await self.aphase("released")

    def _baseline_claim_all(self, client, pools):
        """create_sandbox, get_pod_name and get_pod_ip for every claim at once, like
        agent-sandbox-rl's SandboxFleet.acquire."""
        r = self.result
        n = len(pools)
        # Monotonic Ready time and claim name per claim, by index into ``pools``.
        ready_at, names, sandboxes, errors = [None] * n, [None] * n, [None] * n, []
        t0 = time.monotonic()

        def one(i):
            try:
                sandbox = client.create_sandbox(pools[i], namespace=NS, sandbox_ready_timeout=900)
                ready_at[i] = time.monotonic()
                names[i] = sandbox.claim_name
                sandbox.get_pod_name()
                sandbox.get_pod_ip()
                sandboxes[i] = sandbox
            except Exception as e:
                errors.append(repr(e))

        with ThreadPoolExecutor(n) as pool:
            list(pool.map(one, range(n)))
        r["all_ready_s"] = time.monotonic() - t0
        r["member_ready_s"] = _dist([t - t0 for t in ready_at if t is not None])
        size = self.spec.get("group_size")
        if size:
            # A group is Ready when its pool's last claim is; claim_pools lists each pool's claims together.
            groups = [ready_at[g:g + size] for g in range(0, n, size)]
            r["groups_ready"] = _dist([max(g) - t0 for g in groups if None not in g])
        self._baseline_ready = {name: t for name, t in zip(names, ready_at) if name is not None}
        r["errors"] = errors[:5]
        r["error_count"] = len(errors)
        return t0, [s for s in sandboxes if s is not None]

    def _baseline_terminate_all(self, sandboxes):
        self.cleanup_start("terminate", "pods")
        t = time.monotonic()
        with ThreadPoolExecutor(max(1, len(sandboxes))) as pool:
            list(pool.map(lambda s: s.terminate(), sandboxes))
        self.result["release_s"] = time.monotonic() - t

    def s_baseline(self):
        spec, r = self.spec, self.result
        client = self.make_baseline_client()
        self.phase("start")
        req0 = self.requests.snapshot()
        cpu0 = self.cpu()
        t0, sandboxes = self._baseline_claim_all(client, spec["claim_pools"])
        r["ready_cpu_s"] = self.cpu() - cpu0
        self.fill_stats(t0, len(spec["claim_pools"]))
        self.adoption_stats(self._baseline_ready, t0)
        r["requests_until_ready"] = self.requests.since(self.requests.snapshot(), req0)
        self.phase("ready")
        self._baseline_terminate_all(sandboxes)
        self.phase("released")

    def _rounds(self, run_one, members):
        """Runs ``rounds`` rounds of one command on every member, 20 at a time, and records round 1
        (the fan-out that opens connections) apart from rounds 2 onward."""
        spec, r = self.spec, self.result
        self.sampler.track_pod_tuples = True
        per_round = []
        for k in range(spec["rounds"]):
            gets0 = self.requests.snapshot().get("get sandboxes", 0)
            opens0 = _tcp_active_opens()
            latencies, errors = [], []
            t = time.monotonic()
            with ThreadPoolExecutor(spec["concurrency"]) as pool:
                for result in pool.map(run_one, members):
                    (latencies if isinstance(result, float) else errors).append(result)
            opens1 = _tcp_active_opens()
            self.sampler.sample()
            per_round.append({
                "round": k + 1, "wall_s": time.monotonic() - t, "latencies_ms": latencies, "errors": errors,
                "sandbox_gets": self.requests.snapshot().get("get sandboxes", 0) - gets0,
                "tcp_active_opens": None if opens0 is None else opens1 - opens0,
                "pod_conns_seen_total": len(self.sampler.pod_tuples),
            })
        self._summarize_rounds(per_round, len(members))

    def _summarize_rounds(self, per_round, n):
        r = self.result
        r["members"] = n

        def summary(rounds):
            lat = [x for rd in rounds for x in rd["latencies_ms"]]
            opens = [rd["tcp_active_opens"] for rd in rounds]
            return {"commands": len(lat), "errors": sum(len(rd["errors"]) for rd in rounds),
                    "error_samples": [e for rd in rounds for e in rd["errors"]][:3],
                    "p50_ms": _percentile(lat, 50), "p99_ms": _percentile(lat, 99),
                    "sandbox_gets": sum(rd["sandbox_gets"] for rd in rounds),
                    "tcp_active_opens": None if None in opens else sum(opens)}

        r["round1"] = summary(per_round[:1])
        r["rounds2plus"] = summary(per_round[1:])
        r["distinct_pod_ip_conns_seen"] = len(self.sampler.pod_tuples)
        r["distinct_pod_ips_seen"] = len({t[1] for t in self.sampler.pod_tuples})
        r["pod_conns_peak"] = self.sampler.pod_conns_peak
        r["per_round"] = [{k: v for k, v in rd.items() if k != "latencies_ms"} | {"p50_ms": _percentile(rd["latencies_ms"], 50)}
                          for rd in per_round]

    def s_dataplane(self):
        spec, r = self.spec, self.result
        client = self.make_client()
        client.k8s_helper.api_pool_size()
        t0 = time.monotonic()
        batch = client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        self.sampler.batch = batch
        members = batch.wait_for_quorum()
        r["all_ready_s"] = time.monotonic() - t0
        self.phase("ready")

        def one(member):
            sandbox = batch.connect(member)
            t = time.perf_counter()
            try:
                result = sandbox.commands.run("echo ok")
            except Exception as e:
                return repr(e)
            return (time.perf_counter() - t) * 1000 if result.exit_code == 0 else f"exit {result.exit_code}"

        self._rounds(one, members)
        self.phase("rounds_done")
        self.cleanup_start("release", "pods")
        t = time.monotonic()
        batch.release()
        r["release_s"] = time.monotonic() - t
        self.phase("released")

    async def a_dataplane(self):
        spec, r = self.spec, self.result
        client = self.make_client()
        await client.k8s_helper.api_pool_size()
        t0 = time.monotonic()
        batch = await client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        self.sampler.batch = batch
        members = await batch.wait_for_quorum()
        r["all_ready_s"] = time.monotonic() - t0
        await self.aphase("ready")
        self.sampler.track_pod_tuples = True
        gate = asyncio.Semaphore(spec["concurrency"])

        async def one(member):
            async with gate:
                sandbox = await batch.connect(member)
                t = time.perf_counter()
                try:
                    result = await sandbox.commands.run("echo ok")
                except Exception as e:
                    return repr(e)
                return (time.perf_counter() - t) * 1000 if result.exit_code == 0 else f"exit {result.exit_code}"

        per_round = []
        for k in range(spec["rounds"]):
            gets0 = self.requests.snapshot().get("get sandboxes", 0)
            opens0 = _tcp_active_opens()
            t = time.monotonic()
            results = await asyncio.gather(*(one(m) for m in members))
            opens1 = _tcp_active_opens()
            self.sampler.sample()
            per_round.append({
                "round": k + 1, "wall_s": time.monotonic() - t,
                "latencies_ms": [x for x in results if isinstance(x, float)],
                "errors": [x for x in results if not isinstance(x, float)],
                "sandbox_gets": self.requests.snapshot().get("get sandboxes", 0) - gets0,
                "tcp_active_opens": None if opens0 is None else opens1 - opens0,
                "pod_conns_seen_total": len(self.sampler.pod_tuples),
            })
        self._summarize_rounds(per_round, len(members))
        await self.aphase("rounds_done")
        await self.acleanup_start("release", "pods")
        t = time.monotonic()
        await batch.release()
        r["release_s"] = time.monotonic() - t
        await client.close()
        await self.aphase("released")

    def s_baseline_dataplane(self):
        spec, r = self.spec, self.result
        client = self.make_baseline_client()
        _, sandboxes = self._baseline_claim_all(client, spec["claim_pools"])
        self.phase("ready")

        def one(sandbox):
            t = time.perf_counter()
            try:
                result = sandbox.commands.run("echo ok")
            except Exception as e:
                return repr(e)
            return (time.perf_counter() - t) * 1000 if result.exit_code == 0 else f"exit {result.exit_code}"

        self._rounds(one, sandboxes)
        self.phase("rounds_done")
        self._baseline_terminate_all(sandboxes)
        self.phase("released")

    def _rolling_summary(self, t0, acquire_ms, completed, req0, cpu0, errors):
        r = self.result
        wall = time.monotonic() - t0
        r["rollouts"] = completed
        r["rollout_errors"] = len(errors)
        r["rollout_error_samples"] = errors[:5]
        r["rolling_wall_s"] = wall
        r["rollouts_per_min"] = completed / wall * 60
        r["acquire_ms"] = _dist(acquire_ms)
        r["rolling_cpu_s"] = self.cpu() - cpu0
        r["rolling_cpu_cores"] = r["rolling_cpu_s"] / wall
        reqs = self.requests.since(self.requests.snapshot(), req0)
        r["requests_rolling"] = reqs
        r["requests_per_rollout"] = sum(reqs.values()) / completed if completed else None

    def s_rolling(self):
        spec = self.spec
        client = self.make_client()
        client.k8s_helper.api_pool_size()
        pool = spec["groups"][0][0]
        batch = client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        self.sampler.batch = batch
        self.phase("start")
        lock = threading.Lock()
        started = [0]
        acquire_ms, errors, done = [], [], []
        req0, cpu0, t0 = self.requests.snapshot(), self.cpu(), time.monotonic()

        def worker():
            while True:
                with lock:
                    if started[0] >= spec["rollouts"]:
                        return
                    started[0] += 1
                t = time.perf_counter()
                try:
                    member = batch.acquire(pool)
                    acquire_ms.append((time.perf_counter() - t) * 1000)
                    batch.connect(member).commands.run("echo ok")
                    time.sleep(spec["hold_s"])
                    batch.release_member(member)
                    done.append(1)
                except Exception as e:
                    errors.append(repr(e))

        with ThreadPoolExecutor(spec["concurrency"]) as workers:
            for _ in range(spec["concurrency"]):
                workers.submit(worker)
        self._rolling_summary(t0, acquire_ms, len(done), req0, cpu0, errors)
        self.result["members_at_end"] = len(batch.members())
        self.phase("rolled")
        self.cleanup_start("release", "pods")
        t = time.monotonic()
        batch.release()
        self.result["release_s"] = time.monotonic() - t
        self.phase("released")

    def malloc_probe(self):
        """RSS and mallinfo2() before and after malloc_trim(0). Free memory that malloc_trim hands
        back to the OS points at fragmentation; malloc's in-use bytes (uordblks plus hblkhd) near
        RSS point at live allocations. RSS above malloc's arena plus hblkhd is pymalloc or other
        mmapped memory, which neither number sees."""
        libc = _libc()
        before = {"rss_mb": self.proc.memory_info().rss / 2**20, "mallinfo2": _mallinfo2()}
        released = None
        if libc is not None and hasattr(libc, "malloc_trim"):
            released = libc.malloc_trim(0)
        after = {"rss_mb": self.proc.memory_info().rss / 2**20, "mallinfo2": _mallinfo2()}
        self.result["malloc_probe"] = {"before_trim": before, "malloc_trim_returned": released, "after_trim": after}

    def mem_point(self, rollouts):
        """A tracemalloc snapshot, live objects by type, asyncio tasks and RSS, for memdiag."""
        import gc
        import tracemalloc
        from collections import Counter

        gc.collect()
        # Counted before the snapshot, and the snapshot kept on disk, since a snapshot in memory
        # holds one tuple per trace and would show up as growth at the next point.
        types = dict(Counter(type(o).__name__ for o in gc.get_objects()))
        point = {"rollouts": rollouts, "types": types, "tasks": len(asyncio.all_tasks()),
                 "rss_mb": self.proc.memory_info().rss / 2**20, "traced_mb": tracemalloc.get_traced_memory()[0] / 2**20}
        point["snap"] = f"/tmp/memdiag-{os.getpid()}-{rollouts}.snap"
        tracemalloc.take_snapshot().filter_traces([tracemalloc.Filter(False, tracemalloc.__file__)]).dump(point["snap"])
        gc.collect()
        return point

    def record_memdiag(self, a, b):
        # Skips what mem_point itself allocated, such as the first point's type counts.
        code = Driver.mem_point.__code__
        own = range(code.co_firstlineno, code.co_firstlineno + 30)
        import tracemalloc

        stats = [st for st in tracemalloc.Snapshot.load(b["snap"]).compare_to(tracemalloc.Snapshot.load(a["snap"]), "traceback")
                 if not any(f.filename == code.co_filename and f.lineno in own for f in st.traceback)]
        top = []
        for stat in stats[:20]:
            frames = list(stat.traceback)[-5:][::-1]
            top.append({"size_diff_kb": stat.size_diff / 1024, "count_diff": stat.count_diff,
                        "frames_innermost_first": [f"{f.filename}:{f.lineno}" for f in frames]})
        growth = sorted(((name, b["types"].get(name, 0) - a["types"].get(name, 0)) for name in b["types"] | a["types"]),
                        key=lambda kv: -kv[1])[:20]
        self.result["memdiag"] = {
            "points": [{k: v for k, v in p.items() if k not in ("snap", "types")} for p in (a, b)],
            "top_traceback_growth": top,
            "top_type_growth": [{"type": name, "count_diff": d, "count_at_end": b["types"].get(name, 0)} for name, d in growth],
        }

    async def a_rolling(self):
        spec = self.spec
        client = self.make_client()
        await client.k8s_helper.api_pool_size()
        pool = spec["groups"][0][0]
        memdiag_at = spec.get("memdiag_at")
        if memdiag_at:
            import tracemalloc

            tracemalloc.start(10)
        batch = await client.claim_batch(self.groups_of(spec), namespace=NS, **self.batch_kwargs())
        self.sampler.batch = batch
        await self.aphase("start")
        started = [0]
        acquire_ms, errors, done = [], [], []
        points = []
        req0, cpu0, t0 = self.requests.snapshot(), self.cpu(), time.monotonic()

        async def worker():
            while started[0] < spec["rollouts"]:
                started[0] += 1
                t = time.perf_counter()
                try:
                    member = await batch.acquire(pool)
                    acquire_ms.append((time.perf_counter() - t) * 1000)
                    sandbox = await batch.connect(member)
                    await sandbox.commands.run("echo ok")
                    await asyncio.sleep(spec["hold_s"])
                    await batch.release_member(member)
                    done.append(1)
                    if memdiag_at and len(done) == memdiag_at[0]:
                        points.append(self.mem_point(len(done)))
                except Exception as e:
                    errors.append(repr(e))

        await asyncio.gather(*(worker() for _ in range(spec["concurrency"])))
        self._rolling_summary(t0, acquire_ms, len(done), req0, cpu0, errors)
        self.result["members_at_end"] = len(batch.members())
        if memdiag_at and points:
            # The second point is after the last rollout and before release().
            self.record_memdiag(points[0], self.mem_point(len(done)))
            tracemalloc.stop()
        await self.aphase("rolled")
        await self.acleanup_start("release", "pods")
        t = time.monotonic()
        await batch.release()
        self.result["release_s"] = time.monotonic() - t
        await client.close()
        await self.aphase("released")

    def s_baseline_rolling(self):
        spec = self.spec
        client = self.make_baseline_client()
        pool = spec["claim_pools"][0]
        self.phase("start")
        lock = threading.Lock()
        started = [0]
        acquire_ms, errors, done = [], [], []
        req0, cpu0, t0 = self.requests.snapshot(), self.cpu(), time.monotonic()

        def worker():
            while True:
                with lock:
                    if started[0] >= spec["rollouts"]:
                        return
                    started[0] += 1
                t = time.perf_counter()
                try:
                    sandbox = client.create_sandbox(pool, namespace=NS, sandbox_ready_timeout=900)
                    sandbox.get_pod_name()
                    sandbox.get_pod_ip()
                    acquire_ms.append((time.perf_counter() - t) * 1000)
                    sandbox.commands.run("echo ok")
                    time.sleep(spec["hold_s"])
                    sandbox.terminate()
                    done.append(1)
                except Exception as e:
                    errors.append(repr(e))

        with ThreadPoolExecutor(spec["concurrency"]) as workers:
            for _ in range(spec["concurrency"]):
                workers.submit(worker)
        self._rolling_summary(t0, acquire_ms, len(done), req0, cpu0, errors)
        self.phase("rolled")
        self.phase("released")

    async def a_baseline_rolling(self):
        """The baseline's rolling loop on the base code's AsyncSandboxClient, with an injected
        ApiClient whose pool is ``baseline_pool``."""
        from kubernetes_asyncio import client as k8s, config

        from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient

        spec = self.spec
        try:
            config.load_incluster_config()
        except config.ConfigException:
            await config.load_kube_config()
        configuration = k8s.Configuration.get_default_copy()
        configuration.connection_pool_maxsize = spec["baseline_pool"]
        client = AsyncSandboxClient(connection_config=self.connection_config(), api_client=k8s.ApiClient(configuration=configuration))
        self.probe.install(client.k8s_helper)
        self.client = client
        pool = spec["claim_pools"][0]
        await self.aphase("start")
        started = [0]
        acquire_ms, errors, done = [], [], []
        req0, cpu0, t0 = self.requests.snapshot(), self.cpu(), time.monotonic()

        async def worker():
            while started[0] < spec["rollouts"]:
                started[0] += 1
                t = time.perf_counter()
                try:
                    sandbox = await client.create_sandbox(pool, namespace=NS, sandbox_ready_timeout=900)
                    await sandbox.get_pod_name()
                    await sandbox.get_pod_ip()
                    acquire_ms.append((time.perf_counter() - t) * 1000)
                    await sandbox.commands.run("echo ok")
                    await asyncio.sleep(spec["hold_s"])
                    await sandbox.terminate()
                    done.append(1)
                except Exception as e:
                    errors.append(repr(e))

        await asyncio.gather(*(worker() for _ in range(spec["concurrency"])))
        self._rolling_summary(t0, acquire_ms, len(done), req0, cpu0, errors)
        # On the sampler's clock, so the RSS series can be cut to the rolling phase.
        self.result["rolling_start_s"] = t0 - self.sampler.started
        self.result["rolling_end_s"] = time.monotonic() - self.sampler.started
        if spec.get("malloc_probe"):
            self.malloc_probe()
        await self.aphase("rolled")
        await client.close()
        await self.aphase("released")


# ---------------------------------------------------------------------------
# Orchestrator side
# ---------------------------------------------------------------------------


def kubectl(*args, input=None, check=True):
    return subprocess.run(["kubectl", *args], input=input, capture_output=True, text=True, check=check).stdout


_K8S = {}


def k8s():
    """The orchestrator's own Kubernetes clients, on the host."""
    if not _K8S:
        from kubernetes import client, config

        config.load_kube_config()
        _K8S["core"] = client.CoreV1Api()
        _K8S["custom"] = client.CustomObjectsApi()
    return _K8S


def list_custom(group, plural, **kwargs):
    return k8s()["custom"].list_namespaced_custom_object(group, "v1beta1", NS, plural, **kwargs)


def count_claims():
    """The number of claims in the namespace, from a one-item list."""
    out = list_custom("extensions.agents.x-k8s.io", "sandboxclaims", limit=1)
    return len(out["items"]) + (out["metadata"].get("remainingItemCount") or 0)


def count_pods():
    out = k8s()["core"].list_namespaced_pod(NS, limit=1)
    return len(out.items) + (out.metadata.remaining_item_count or 0)


def claim_pod_names(claim_names=None):
    """Pods of Sandboxes owned by claims, or by the given claims."""
    sandboxes = list_custom("agents.x-k8s.io", "sandboxes")["items"]
    owned = set()
    for s in sandboxes:
        for ref in s["metadata"].get("ownerReferences") or []:
            if ref["kind"] == "SandboxClaim" and (claim_names is None or ref["name"] in claim_names):
                owned.add(s["metadata"]["name"])
    names = set()
    for p in k8s()["core"].list_namespaced_pod(NS).items:
        for ref in p.metadata.owner_references or []:
            if ref.kind == "Sandbox" and ref.name in owned:
                names.add(p.metadata.name)
    return names


class CleanupTracker(threading.Thread):
    """Times how long a release's claims take to go (polled every 1 s), and in ``pods`` mode its
    pods too. In ``count`` mode it records how many pods are left once the claims are gone."""

    def __init__(self, mode, selector):
        super().__init__(daemon=True)
        self.mode = mode
        self.selector = selector
        self.result = {"mode": mode, "selector": selector or None}
        if selector:
            items = list_custom("extensions.agents.x-k8s.io", "sandboxclaims", label_selector=selector)["items"]
            self.claims = {c["metadata"]["name"] for c in items}
            self.result["claims_at_start"] = len(self.claims)
        else:
            self.claims = None
            self.result["claims_at_start"] = count_claims()
        self.pods = claim_pod_names(self.claims) if mode == "pods" else None
        if self.pods is not None:
            self.result["pods_at_start"] = len(self.pods)
        self.t0 = time.monotonic()

    def claims_left(self):
        if self.claims is None:
            return count_claims()
        items = list_custom("extensions.agents.x-k8s.io", "sandboxclaims", label_selector=self.selector)["items"]
        return len(items)

    def run(self):
        try:
            while self.claims_left():
                time.sleep(1)
            self.result["claims_gone_s"] = time.monotonic() - self.t0
            if self.pods is None:
                self.result["pods_left_at_claims_gone"] = count_pods()
                return
            while True:
                left = self.pods & {p.metadata.name for p in k8s()["core"].list_namespaced_pod(NS).items}
                if "pods_left_at_claims_gone" not in self.result:
                    self.result["pods_left_at_claims_gone"] = len(left)
                if not left:
                    break
                time.sleep(1)
            self.result["pods_gone_s"] = time.monotonic() - self.t0
        except Exception as e:
            self.result["error"] = repr(e)


POST_CLAIMS = "post sandboxclaims duration"


def scrape():
    """The apiserver counters this benchmark diffs: totals, 429s, flow-control rejections, and
    the POST sandboxclaims latency histogram (cumulative buckets by le, sum and count)."""
    from prometheus_client.parser import text_string_to_metric_families

    # Present even when no series has the label yet, so a run's diff shows 0 rather than nothing.
    out = {"429s": 0, "flowcontrol_rejected": 0, f"{POST_CLAIMS} _sum": 0, f"{POST_CLAIMS} _count": 0}
    for family in text_string_to_metric_families(kubectl("get", "--raw", "/metrics")):
        for s in family.samples:
            labels = s.labels
            if s.name.startswith("apiserver_request_duration_seconds_") and labels.get("resource") == "sandboxclaims" \
                    and labels.get("verb") == "POST" and not labels.get("subresource"):
                suffix = s.name.rsplit("_", 1)[1]
                key = f"{POST_CLAIMS} le={labels['le']}" if suffix == "bucket" else f"{POST_CLAIMS} _{suffix}"
                out[key] = out.get(key, 0) + s.value
            elif s.name == "apiserver_request_total":
                out["requests_all"] = out.get("requests_all", 0) + s.value
                if labels.get("code") == "429":
                    out["429s"] = out.get("429s", 0) + s.value
                if labels.get("resource") in ("sandboxclaims", "leases", "sandboxes"):
                    key = f"requests {labels.get('verb')} {labels.get('resource')}"
                    out[key] = out.get(key, 0) + s.value
            elif s.name == "apiserver_flowcontrol_rejected_requests_total":
                out["flowcontrol_rejected"] = out.get("flowcontrol_rejected", 0) + s.value
    return out


def diff(after, before):
    # Every scraped key stays, at 0 when it didn't change.
    return {k: after.get(k, 0) - before.get(k, 0) for k in after.keys() | before.keys()}


def post_claims_latency(d):
    """Count, mean and the p99's bucket (its upper bound, in seconds) from a diff of the histogram."""
    count = d.get(f"{POST_CLAIMS} _count", 0)
    if not count:
        return {"count": 0, "mean_s": None, "p99_le_s": None}
    buckets = sorted((float(k.rsplit("le=", 1)[1]), v) for k, v in d.items() if k.startswith(f"{POST_CLAIMS} le="))
    p99 = next(le for le, cum in buckets if cum >= 0.99 * count)
    return {"count": count, "mean_s": d.get(f"{POST_CLAIMS} _sum", 0) / count, "p99_le_s": p99}


class Gauges(threading.Thread):
    """Samples the apiserver's in-flight WATCHes on sandboxclaims and the cluster's pod count every
    second, and the warm pool's Ready unadopted pods every ``warm_interval`` seconds."""

    def __init__(self, warm_pool, warm_interval=10):
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.warm_pool = warm_pool
        self.warm_interval = warm_interval
        self.watch_peak = 0
        self.watch_idle = self.watches()
        self.pods_peak = 0
        self.warm_series = []
        self.t0 = time.monotonic()

    @staticmethod
    def watches():
        total = 0
        for line in kubectl("get", "--raw", "/metrics", check=False).splitlines():
            if line.startswith("apiserver_longrunning_requests{") and 'resource="sandboxclaims"' in line and 'verb="WATCH"' in line:
                total += float(line.rsplit(" ", 1)[1])
        return total

    def warm_ready(self):
        pools = warm_ready_pods()
        return pools.get(self.warm_pool, 0)

    def run(self):
        t0 = self.t0
        next_warm = 0.0
        while not self.stop.is_set():
            try:
                self.watch_peak = max(self.watch_peak, self.watches())
                out = k8s()["core"].list_pod_for_all_namespaces(limit=1)
                self.pods_peak = max(self.pods_peak, len(out.items) + (out.metadata.remaining_item_count or 0))
                if self.warm_pool and time.monotonic() - t0 >= next_warm:
                    self.warm_series.append((round(time.monotonic() - t0, 1), self.warm_ready()))
                    next_warm = time.monotonic() - t0 + self.warm_interval
            except Exception:
                pass
            self.stop.wait(1)


def warm_ready_pods():
    """Ready pods of unadopted warm Sandboxes, per warm pool."""
    owner = {}
    for s in list_custom("agents.x-k8s.io", "sandboxes")["items"]:
        for ref in s["metadata"].get("ownerReferences") or []:
            if ref["kind"] == "SandboxWarmPool":
                owner[s["metadata"]["name"]] = ref["name"]
    counts = {}
    for p in k8s()["core"].list_namespaced_pod(NS).items:
        ready = any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or []))
        for ref in p.metadata.owner_references or []:
            if ref.kind == "Sandbox" and ref.name in owner and ready:
                counts[owner[ref.name]] = counts.get(owner[ref.name], 0) + 1
    return counts


def prepare_pools(extras, log):
    """Makes the extra templates and warm pools exactly ``extras`` ({pool: (template, shape, replicas)})."""
    want = {name: spec for name, spec in extras.items()}
    have_pools = {p["metadata"]["name"]: p["spec"].get("replicas")
                  for p in list_custom("extensions.agents.x-k8s.io", "sandboxwarmpools", label_selector=f"{EXTRA_LABEL}=true")["items"]}
    if have_pools != {name: s[2] for name, s in want.items()}:
        if have_pools:
            log(f"deleting {len(have_pools)} extra pools")
            kubectl("delete", "sandboxwarmpools,sandboxtemplates", "-n", NS, "-l", f"{EXTRA_LABEL}=true", "--wait=true", check=False)
        if want:
            log(f"creating {len(want)} extra pools")
            docs = []
            for name, (template, shape, replicas) in want.items():
                # "existing": a warm pool on one of the setup templates, which isn't labeled extra.
                if shape != "existing":
                    docs.append(template_manifest(template, shape, extra=True))
                docs.append(pool_manifest(name, template, replicas, extra=True))
            for i in range(0, len(docs), 400):
                kubectl("apply", "-f", "-", input="---\n".join(docs[i:i + 400]))
    return sum(s[2] for s in want.values())


def gc_queue_depth():
    """kube-controller-manager's garbage_collector_attempt_to_delete queue depth, or None."""
    node = os.environ.get("BENCH_KIND_NODE", "batch-bench-control-plane")
    out = subprocess.run(["bash", os.path.join(HERE, "gc-queue.sh"), node], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith('workqueue_depth{name="garbage_collector_attempt_to_delete"}'):
            return float(line.rsplit(" ", 1)[1])
    return None


def wait_clean(warm_total, log, timeout=7200):
    """Waits for no claims, Sandboxes or pods but the warm pools' Ready ones. Once no claim is left,
    deletes leftover Sandboxes and pods directly, since the garbage collector deletes a large run's
    at a few per second. No run measures that."""
    deadline = time.monotonic() + timeout
    while True:
        claims = count_claims()
        sandboxes = list_custom("agents.x-k8s.io", "sandboxes")["items"]
        warm = {s["metadata"]["name"] for s in sandboxes
                if any(r["kind"] == "SandboxWarmPool" for r in s["metadata"].get("ownerReferences") or [])}
        pods = k8s()["core"].list_namespaced_pod(NS).items
        stray_sb = [s["metadata"]["name"] for s in sandboxes if s["metadata"]["name"] not in warm]
        stray_pods = [p.metadata.name for p in pods
                      if not any(r.kind == "Sandbox" and r.name in warm for r in p.metadata.owner_references or [])]
        ready = sum(1 for p in pods if p.metadata.name not in stray_pods
                    and any(c.type == "Ready" and c.status == "True" for c in (p.status.conditions or [])))
        lease_items = kubectl("get", "leases", "-n", NS, "-o", "name", check=False).split()
        if not claims and not stray_sb and not stray_pods and not lease_items and len(warm) == warm_total and ready == warm_total:
            # The garbage collector works through a large run's deletes for a long time after the
            # objects are gone, and a later run's cleanup would queue behind them.
            depth = gc_queue_depth()
            if depth is None or depth < 50:
                return
            log(f"waiting for the garbage collector's queue to drain ({depth:.0f} items)")
            time.sleep(15)
            continue
        if not claims:
            if not warm_total and (stray_sb or stray_pods):
                kubectl("delete", "sandboxes.agents.x-k8s.io,pods", "--all", "-n", NS, "--wait=false", check=False)
            else:
                # Unscheduled pods in one request: warm pods are always on the node, so it spares
                # them. Anything else goes 200 names at a time, then the loop lists again, since
                # kubectl deletes names one request at a time and the garbage collector is
                # removing the same objects meanwhile.
                kubectl("delete", "pods", "-n", NS, "--field-selector", "spec.nodeName=", "--wait=false", check=False)
                if stray_sb:
                    kubectl("delete", "sandboxes.agents.x-k8s.io", "-n", NS, "--wait=false", "--ignore-not-found", *stray_sb[:200], check=False)
                scheduled = [p.metadata.name for p in pods if p.metadata.name in stray_pods and p.spec.node_name]
                if scheduled:
                    kubectl("delete", "pods", "-n", NS, "--wait=false", "--ignore-not-found", *scheduled[:200], check=False)
        if time.monotonic() > deadline:
            raise RuntimeError(f"namespace not clean after {timeout}s: claims={claims} stray sandboxes={len(stray_sb)} "
                               f"stray pods={len(stray_pods)} warm={len(warm)}/{warm_total} ready={ready} leases={len(lease_items)}")
        time.sleep(3)


def run_driver(spec, log_file):
    payload = json.dumps(spec)
    script = f"{POD_REPO_ROOT}/test/benchmarks/batch-claim/bench.py" if spec["location"] == "pod" else os.path.abspath(__file__)
    # A code_sha run uses its commit's venv, installed by setup-venvs.sh sdk-pod.
    python = f"/opt/venv-{spec['code_sha'][:7]}/bin/python" if spec.get("code_sha") else PYTHONS[(spec["location"], spec["code"])]
    cmd = [python, script, "run", payload]
    if spec.get("memray") is not None:
        # The capture stays in the driver's filesystem, so it's copied out by hand.
        label = run_label(spec["scenario"], spec["variant"], spec["shell"])
        cmd = [python, "-m", "memray", "run", "-f", *spec["memray"], "-o", f"/tmp/{label}.bin", *cmd[1:]]
    # Through env(1), since the orchestrator's environment doesn't reach a driver in the pod.
    env = spec.get("env") or {}
    if env:
        cmd = ["env", *(f"{k}={v}" for k, v in env.items()), *cmd]
    if spec["location"] == "pod":
        cmd = ["kubectl", "exec", "-i", "-n", DRIVER_NS, DRIVER_POD, "--", *cmd]
    gauges = Gauges(spec.get("warm_series_pool"), spec.get("warm_interval_s", 10))
    gauges.start()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    phases, phase_at, trackers = {}, {}, {}
    result = None
    started = time.monotonic()
    for line in proc.stdout:
        if line.startswith("@@PHASE "):
            phase_at[line.split()[1]] = time.monotonic() - gauges.t0
            phases[line.split()[1]] = scrape()
        elif line.startswith("@@CLEANUP "):
            parts = line.split()
            tracker = CleanupTracker(parts[2], parts[3] if len(parts) > 3 else "")
            trackers[parts[1]] = tracker
            tracker.start()
        elif line.startswith("@@RESULT "):
            result = json.loads(line[len("@@RESULT "):])
            continue
        else:
            log_file.write(line)
            log_file.flush()
            continue
        proc.stdin.write("\n")
        proc.stdin.flush()
    proc.wait()
    for tracker in trackers.values():
        tracker.join()
    gauges.stop.set()
    gauges.join()
    if result is None:
        raise RuntimeError(f"driver exited {proc.returncode} without a result")
    result["driver_exit"] = proc.returncode
    result["driver_cmd"] = cmd
    result["run_wall_s"] = time.monotonic() - started
    names = list(phases)
    result["apiserver"] = {f"{a}->{b}": diff(phases[b], phases[a]) for a, b in zip(names, names[1:])}
    if len(names) > 1:
        result["apiserver"]["total"] = diff(phases[names[-1]], phases[names[0]])
    result["cleanup"] = {tag: t.result for tag, t in trackers.items()}
    result["apiserver_watch_peak"] = gauges.watch_peak
    result["apiserver_watch_idle"] = gauges.watch_idle
    result["cluster_pods_peak"] = gauges.pods_peak
    if "total" in result["apiserver"]:
        result["apiserver_post_claims"] = post_claims_latency(result["apiserver"]["total"])
    if spec.get("warm_series_pool"):
        result["warm_ready_series"] = gauges.warm_series
        # The minimum while the rollouts ran, from the start phase to the rolled one.
        lo, hi = phase_at.get("start", 0), phase_at.get("rolled", float("inf"))
        during = [v for t, v in gauges.warm_series if lo <= t <= hi]
        result["warm_ready_min"] = min(during) if during else None
        result["starved"] = during != [] and min(during) == 0
    return result


def ready_n_cold():
    return 300


def read_candidate():
    path = os.path.join(RESULTS_DIR, "candidate.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def pacing_of(variant):
    """(create_rps, max_in_flight) from a variant named batch-rps<C>-mif<M>."""
    rps, mif = variant.removeprefix("batch-rps").split("-mif")
    return int(rps), int(mif)


def run_plan():
    """Every run, as (scenario, variant, shell, runs, spec), in the order 3, 6, 1, 5, 2, 4, then
    the 2026-10 rerun's 3s, 3a2, 6v2, 1d."""
    plan = []

    def add(scenario, variant, shell, spec, runs=3):
        plan.append((scenario, variant, shell, 1 if SMOKE else runs,
                     {"scenario": scenario, "variant": variant, "shell": shell, **spec}))

    tip_pod = {"location": "pod", "code": "tip"}
    base_pod = {"location": "pod", "code": "base"}
    tip_host = {"location": "host", "code": "tip"}
    n_cold, n_warm = ready_n_cold(), 160
    warm = {WARM_POOL: ("ready-template", "existing", n_warm)}
    rl_pools = {f"w{i}-pool": (f"w{i}-tmpl", "ready", 4) for i in range(n_warm // 4)}

    def baselines(scenario, pools, extras):
        add(scenario, "baseline-pool1000", "sync", {**base_pod, "kind": "baseline", "claim_pools": pools,
                                                     "concurrency": len(pools), "baseline_pool": 1000, "extras": extras})
        add(scenario, "baseline-defaultpool", "sync", {**base_pod, "kind": "baseline", "claim_pools": pools,
                                                        "concurrency": len(pools), "baseline_pool": None, "extras": extras}, runs=1)

    # 3. Ready path versus the baseline.
    for rps in (None, 1000):
        add("3a", f"batch-rps{rps or 'default'}", "sync", {**tip_pod, "kind": "ready", "groups": [(COLD_POOL, n_cold)],
                                                           "create_rps": rps, "pool_size": 22, "extras": {}})
    baselines("3a", [COLD_POOL] * n_cold, {})
    for rps in (None, 1000):
        add("3b", f"batch-rps{rps or 'default'}", "sync", {**tip_pod, "kind": "ready", "groups": [(WARM_POOL, n_warm)],
                                                           "create_rps": rps, "pool_size": 22, "extras": warm})
    baselines("3b", [WARM_POOL] * n_warm, warm)
    add("3c", "batch-groups", "sync", {**tip_pod, "kind": "ready", "consumer": "groups", "pool_size": 22,
                                       "groups": [(p, 4, 4) for p in rl_pools], "extras": rl_pools})
    baselines("3c", [p for p in rl_pools for _ in range(4)], rl_pools)
    add("3d", "batch-rpsdefault", "async", {**tip_pod, "kind": "ready", "groups": [(COLD_POOL, n_cold)],
                                            "pool_size": 22, "extras": {}})

    # 6. Rolling acquire.
    c = 100
    rolling = {"rollouts": 1000, "hold_s": 5, "concurrency": c, "warm_series_pool": WARM_POOL,
               "extras": {WARM_POOL: ("ready-template", "existing", c)}}
    add("6a", "batch-rpsdefault", "sync", {**tip_pod, **rolling, "kind": "rolling", "groups": [(WARM_POOL, 0)], "pool_size": 22})
    add("6b", "batch-rps1000", "sync", {**tip_pod, **rolling, "kind": "rolling", "groups": [(WARM_POOL, 0)], "pool_size": 22, "create_rps": 1000})
    add("6c", "batch-rpsdefault", "async", {**tip_pod, **rolling, "kind": "rolling", "groups": [(WARM_POOL, 0)], "pool_size": 22})
    add("6", "baseline-pool1000", "sync", {**base_pod, **rolling, "kind": "baseline_rolling", "claim_pools": [WARM_POOL], "baseline_pool": 1000})

    # 1. Fill scale.
    for n in (1000, 5000, 10000):
        add("1a", f"n{n}", "sync", {**tip_host, "kind": "fill", "groups": [(UNSCHED_POOL, n)], "pool_size": 22, "extras": {}})
    for rps in (20, 50, 100, 200, 500):
        add("1b", f"n5000-rps{rps}", "sync", {**tip_host, "kind": "fill", "groups": [(UNSCHED_POOL, 5000)], "create_rps": rps,
                                              "max_in_flight": 50, "pool_size": 52, "extras": {}})
    add("1c", "n5000", "async", {**tip_host, "kind": "fill", "groups": [(UNSCHED_POOL, 5000)], "pool_size": 22, "extras": {}})

    # 5. Many small groups.
    for variant, g, size in (("g1000x4", 1000, 4), ("g100x40", 100, 40)):
        extras = {f"u{i}-pool": (f"u{i}-tmpl", "unsched", 0) for i in range(g)}
        add("5", variant, "sync", {**tip_host, "kind": "fill", "after": "groups", "groups": [(p, size) for p in extras],
                                   "pool_size": 22, "extras": extras})
    add("5", "g1x4000", "sync", {**tip_host, "kind": "fill", "after": "groups", "groups": [(UNSCHED_POOL, 4000)],
                                 "pool_size": 22, "extras": {}})

    # 2. Connection pool sizing, at the largest N scenario 1 held.
    n2 = 10000 if len([r for r in load("1a-n10000-sync") if r.get("driver_exit") == 0]) == 3 else 5000
    common = {**tip_host, "kind": "fill", "groups": [(UNSCHED_POOL, n2)], "max_in_flight": 100, "create_rps": 1000,
              "lease_duration": 6, "hold_min_s": 20, "extras": {},
              # No detach and get_batch: with a 6 s Lease, a detached batch can be re-attached for
              # only 1 s (its grace minus the 5 s skew margin), less than a 10,000-claim list can take.
              "after": "release"}
    for shell in ("sync", "async"):
        add("2a", "nopoolsize", shell, {**common, "kind": "poolcheck", "pool_size": None}, runs=1)
        add("2b", f"n{n2}-pool102", shell, {**common, "pool_size": 102})
        add("2c", f"n{n2}-pool100-nocheck", shell, {**common, "pool_size": 100, "no_pool_check": True})

    # 4. Data-plane connections.
    dp = {"groups": [(COLD_POOL, n_cold)], "rounds": 10, "concurrency": 20, "pool_size": 22, "extras": {}}
    for shell in ("sync", "async"):
        add("4", "batch", shell, {**tip_pod, **dp, "kind": "dataplane"})
    add("4", "baseline-pool1000", "sync", {**base_pod, **dp, "kind": "baseline_dataplane",
                                           "claim_pools": [COLD_POOL] * n_cold, "baseline_pool": 1000})

    # The 2026-10 rerun. 3s: a pacing sweep on the warm and RL shapes, one shape at a time, since
    # prepare_pools replaces the other shape's warm pools.
    s_warm, s_groups, s_cold = (8, 2, 10) if SMOKE else (160, 40, 300)
    sweep = [(50, 20), (100, 20), (200, 20), (500, 20), (1000, 20), (500, 50), (1000, 50)]
    if SMOKE:
        sweep = [(50, 20), (1000, 50)]
    shapes = {
        "warm": ({WARM_POOL: ("ready-template", "existing", s_warm)}, [(WARM_POOL, s_warm, s_warm)], "quorum"),
        "rl": ({f"w{i}-pool": (f"w{i}-tmpl", "ready", 4) for i in range(s_groups)},
               [(f"w{i}-pool", 4, 4) for i in range(s_groups)], "groups"),
    }
    for shape, (extras, groups, consumer) in shapes.items():
        block = {"interleave": f"3s-{shape}", "extras": extras}
        for rps, mif in sweep:
            add("3s", f"{shape}-batch-rps{rps}-mif{mif}", "sync", {**tip_pod, **block, "kind": "ready", "groups": groups,
                                                                   "consumer": consumer, "create_rps": rps,
                                                                   "max_in_flight": mif, "pool_size": mif + 2})
        add("3s", f"{shape}-baseline-pool1000", "sync", {**base_pod, **block, "kind": "baseline",
                                                         "claim_pools": [g[0] for g in groups for _ in range(g[1])],
                                                         "concurrency": sum(g[1] for g in groups), "baseline_pool": 1000,
                                                         "group_size": 4 if shape == "rl" else None})

    # 3a2: the cold check of candidate.json's cold_check_queue (lowest winners first, or the best
    # variant), against 50/s and the baseline. Its first entry interleaves with them.
    cand = read_candidate() or {}
    queue = cand.get("cold_check_queue") or []
    # Until 3s has a queue, none of 3a2 runs, so the queued variant interleaves with the rest.
    cold = {"kind": "ready", "groups": [(COLD_POOL, s_cold)], "extras": {}, "needs_candidate": not queue}
    cold_variants = ["batch-rps50-mif20"] + [v for v in queue[:1] if v != "batch-rps50-mif20"]
    for variant in cold_variants:
        rps, mif = pacing_of(variant)
        add("3a2", variant, "sync", {**tip_pod, **cold, "interleave": "3a2", "create_rps": rps, "max_in_flight": mif,
                                     "pool_size": mif + 2})
    add("3a2", "baseline-pool1000", "sync", {**base_pod, **cold, "interleave": "3a2", "kind": "baseline",
                                             "claim_pools": [COLD_POOL] * s_cold, "concurrency": s_cold, "baseline_pool": 1000})
    for variant in queue[1:2]:
        if variant not in cold_variants:
            rps, mif = pacing_of(variant)
            add("3a2", variant, "sync", {**tip_pod, **cold, "create_rps": rps, "max_in_flight": mif, "pool_size": mif + 2})

    # 6v2: rolling acquire with a warm pool larger than the workers' demand, so acquire isn't
    # waiting on pod refills. The -hold20 variants run only where a variant's pool hit 0 Ready.
    c2, m2, warm2, hold = (4, 20, 12, 2) if SMOKE else (40, 600, 120, 10)
    rolling2 = {"rollouts": m2, "hold_s": hold, "concurrency": c2, "warm_series_pool": WARM_POOL, "warm_interval_s": 4,
                "extras": {WARM_POOL: ("ready-template", "existing", warm2)}}
    batch_rolling = {**tip_pod, **rolling2, "kind": "rolling", "groups": [(WARM_POOL, 0)]}
    best = (cand.get("candidate") or cand.get("best_variant")) if cand.get("final") else None
    variants6 = [("6v2a", "batch-rps50-mif20", "sync"), ("6v2b", "batch-candidate", "sync"), ("6v2c", "batch-rps50-mif20", "async")]
    entries6 = []
    for scenario, variant, shell in variants6:
        pacing = pacing_of(best) if variant == "batch-candidate" and best else (None if variant == "batch-candidate" else pacing_of(variant))
        spec = {**batch_rolling, "needs_candidate": pacing is None}
        if pacing:
            spec.update(create_rps=pacing[0], max_in_flight=pacing[1], pool_size=pacing[1] + 2, candidate_variant=best)
        entries6.append((scenario, variant, shell, spec))
    entries6.append(("6v2", "baseline-pool1000", "sync", {**base_pod, **rolling2, "kind": "baseline_rolling",
                                                          "claim_pools": [WARM_POOL], "baseline_pool": 1000}))
    for scenario, variant, shell, spec in entries6:
        add(scenario, variant, shell, {**spec, "interleave": "6v2"})
    for scenario, variant, shell, spec in entries6:
        add(scenario, f"{variant}-hold20", shell, {**spec, "interleave": "6v2-hold20", "hold_s": 4 if SMOKE else 20,
                                                   "only_if_starved": run_label(scenario, variant, shell)})

    # 6v2 memory diagnosis: 6v2c under tracemalloc, and the base code's async rolling loop.
    add("6v2", "memdiag", "async", {**batch_rolling, "create_rps": 50, "max_in_flight": 20, "pool_size": 22,
                                    "memdiag_at": [5, 20] if SMOKE else [100, 600]}, runs=1)
    add("6v2", "baseline", "async", {**base_pod, **rolling2, "kind": "baseline_rolling", "claim_pools": [WARM_POOL],
                                     "baseline_pool": 1000}, runs=1)
    # Step 7 asks whether the async baseline's RSS growth is glibc arena fragmentation, so it runs
    # the loop longer as shipped and with MALLOC_ARENA_MAX=2, each with a malloc probe at the end
    # of the rolling phase, and once under memray to find the native allocation sites. memray's own
    # memory inflates that run's RSS and mallinfo2, so it's not compared with the other two.
    long_baseline = {**base_pod, **rolling2, "kind": "baseline_rolling", "claim_pools": [WARM_POOL],
                     "baseline_pool": 1000, "rollouts": 20 if SMOKE else 2400, "malloc_probe": True}
    add("6v2", "baseline-long", "async", long_baseline, runs=1)
    add("6v2", "baseline-arena2", "async", {**long_baseline, "env": {"MALLOC_ARENA_MAX": "2"}}, runs=1)
    add("6v2", "baseline-memray", "async", {**long_baseline, "rollouts": 20 if SMOKE else 600,
                                            "memray": ["--native", "--aggregate"]}, runs=1)
    # The same loop on upstream main and on the fix that drops terminated handles, by commit.
    leak = {**long_baseline, "code": "sha", "rollouts": 20 if SMOKE else 600}
    add("6v2", "leakbase", "async", {**leak, "code_sha": LEAK_BASE_SHA}, runs=1)
    add("6v2", "leakfix", "async", {**leak, "code_sha": LEAK_FIX_SHA}, runs=1)
    # Longer, since dropped handles are freed only by the cyclic GC and 600 rollouts can't tell a
    # plateau from growth.
    add("6v2", "leakbase-long", "async", {**leak, "code_sha": LEAK_BASE_SHA, "rollouts": 20 if SMOKE else 2400}, runs=1)
    add("6v2", "leakfix-long", "async", {**leak, "code_sha": LEAK_FIX_SHA, "rollouts": 20 if SMOKE else 2400}, runs=1)

    # 1d: memory of the list and release paths at N = 5,000.
    add("1d", "n5000-memprobe", "sync", {**tip_host, "kind": "fill", "after": "memprobe", "extras": {},
                                         "groups": [(UNSCHED_POOL, 50 if SMOKE else 5000)], "create_rps": 1000,
                                         "max_in_flight": 100, "pool_size": 102, "lease_duration": 60})
    return plan


def run_label(scenario, variant, shell):
    return f"{scenario}-{variant}-{shell}"


def result_path(label, k):
    return os.path.join(RESULTS_DIR, f"{label}-run{k}.json")


def load(label):
    runs = []
    for path in sorted(glob.glob(os.path.join(RESULTS_DIR, f"{label}-run*.json"))):
        with open(path) as f:
            runs.append(json.load(f))
    return runs


STATUS_PATH = os.path.join(RESULTS_DIR, "STATUS.md")


# The 2026-10 rerun's steps, by run-label prefix, for STATUS.md's step summary.
STEPS = {"0 (1a reruns)": ("1a-n1000-sync", "1a-n5000-sync"), "2 (3s)": ("3s-",), "3 (3a2)": ("3a2-",),
         "4 (6v2)": ("6v2a-", "6v2b-", "6v2c-", "6v2-baseline-pool1000"), "5 (memdiag)": ("6v2-memdiag-", "6v2-baseline-async"),
         "6 (1d)": ("1d-",), "7 (memprobe)": ("6v2-baseline-long-", "6v2-baseline-arena2-", "6v2-baseline-memray-"),
         "8 (leakfix)": ("6v2-leakbase-", "6v2-leakfix-")}


def run_errors(r):
    """Why a finished run counts as errored, or None."""
    reasons = [f"{k}={r[k]}" for k in ("error_count", "rollout_errors", "group_error_count") if r.get(k)]
    if r.get("driver_exit") not in (0, None):
        reasons.insert(0, f"driver_exit={r['driver_exit']}")
    return ", ".join(reasons) or None


def superseded_names():
    d = os.path.join(RESULTS_DIR, "superseded")
    return set(os.listdir(d)) if os.path.isdir(d) else set()


def write_status(statuses):
    lines = ["# Run status", "", "One line per run; generated by bench.py suite from the result files.", ""]
    lines += [f"- {key}: {value}" for key, value in statuses.items()]
    lines += ["", "## 2026-10 rerun steps", ""]
    for step, prefixes in STEPS.items():
        keys = [k for k in statuses if k.startswith(prefixes)]
        if not keys:
            continue
        count = lambda word: sum(1 for k in keys if statuses[k].startswith(word))
        flagged = sum(1 for k in keys if ", starved" in statuses[k])
        errored = sum(1 for k in keys if "errors" in statuses[k])
        lines.append(f"- Step {step}: {count('done')}/{len(keys)} done, {count('pending') + count('waiting')} pending, "
                     f"{count('not needed')} not needed, {errored} with errors, {flagged} starved, "
                     f"{sum(1 for k in keys if 'superseded' in statuses[k])} rerun after a superseded attempt")
    with open(STATUS_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")


def run_condition(spec):
    """Why a planned run can't start yet or isn't needed, or None if it can run."""
    if spec.get("needs_candidate"):
        return "waiting for results/candidate.json"
    base = spec.get("only_if_starved")
    if base:
        runs = load(base)
        if any(r.get("starved") for r in runs):
            return None
        planned = next(n for sc, v, sh, n, _ in run_plan() if run_label(sc, v, sh) == base)
        return f"not needed (no starved run in {base})" if len(runs) >= planned else f"waiting for {base}"
    return None


def statuses_from_disk():
    statuses = {}
    gone = superseded_names()
    for scenario, variant, shell, runs, spec in run_plan():
        label = run_label(scenario, variant, shell)
        for k in range(1, runs + 1):
            path = result_path(label, k)
            key = f"{label}-run{k}"
            if os.path.exists(path):
                with open(path) as f:
                    r = json.load(f)
                status = "done (imported)" if r.get("imported") else "done"
                if run_errors(r):
                    status = f"done with errors ({run_errors(r)}, see logs/{key}.log)"
                if r.get("starved"):
                    status += ", starved (warm pool hit 0 Ready)"
            else:
                status = run_condition(spec) or "pending"
            if f"{key}.json" in gone:
                status += ", superseded once (see superseded/WHY.md)"
            statuses[key] = status
    return statuses


def git(*args):
    return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True).stdout.strip()


def tip_sha():
    return git("rev-parse", "--verify", "-q", TIP_BRANCH) or git("rev-parse", f"origin/{TIP_BRANCH}")


def check_code():
    """The tip branch's commit at run time, after checking the pinned commits' trees are the ones
    under test. The branch has moved on since, so only CODE_SHA's tree is checked, not the branch's."""
    tip = git("rev-parse", f"origin/{TIP_BRANCH}")
    trees = git("rev-parse", f"{CODE_SHA}^{{tree}}", f"{BASE_SHA}^{{tree}}").split()
    if trees != [CODE_TREE, BASE_TREE]:
        raise SystemExit(f"trees {trees} aren't {[CODE_TREE, BASE_TREE]}")
    return tip


def check_context():
    """Refuses to run unless BENCH_CONTEXT names the current kubectl context, since bench.py and
    the scripts it calls follow KUBECONFIG and would otherwise run against any cluster."""
    want = os.environ.get("BENCH_CONTEXT")
    have = kubectl("config", "current-context", check=False).strip()
    if not want or want != have:
        raise SystemExit(f"BENCH_CONTEXT is {want!r} but the current context is {have!r}; set BENCH_CONTEXT to the "
                         "benchmark cluster's context and KUBECONFIG to its kubeconfig")


def controller_image():
    pod = json.loads(kubectl("get", "pods", "-n", "agent-sandbox-system", "-l", "app=agent-sandbox-controller", "-o", "json"))["items"][0]
    status = pod["status"]["containerStatuses"][0]
    return {"controller_image": status["image"], "controller_image_id": status["imageID"],
            "controller_pod": pod["metadata"]["name"]}


def record_env():
    """Writes env.json once, and again when the cluster or controller changed; the old one is kept
    as env-<time>.json. Fields an older env.json lacks are added to it."""
    path = os.path.join(RESULTS_DIR, "env.json")
    old = None
    if os.path.exists(path):
        with open(path) as f:
            old = json.load(f)
    node = json.loads(kubectl("get", "node", "-o", "json"))["items"][0]
    deploy = json.loads(kubectl("get", "deploy", "-n", "agent-sandbox-system", "agent-sandbox-controller", "-o", "json"))
    env = {
        "host": platform.platform(),
        "host_cpus": os.cpu_count(),
        "host_ram_bytes": int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0),
        "docker_vm": subprocess.run(["docker", "info", "--format", "{{.NCPU}} cpus {{.MemTotal}} bytes"], capture_output=True, text=True).stdout.strip(),
        "kind": subprocess.run(["kind", "version"], capture_output=True, text=True).stdout.strip(),
        "kubernetes": node["status"]["nodeInfo"]["kubeletVersion"],
        "node_allocatable_pods": node["status"]["allocatable"]["pods"],
        "node_pod_cidr": node["spec"].get("podCIDR"),
        "controller_args": deploy["spec"]["template"]["spec"]["containers"][0].get("args"),
        "controller_image": deploy["spec"]["template"]["spec"]["containers"][0]["image"],
        "controller_image_id": controller_image()["controller_image_id"],
        "node_boot_id": node["status"]["nodeInfo"]["bootID"],
        "tip_branch": TIP_BRANCH,
        "tip_sha": tip_sha(),
        "bench_head_sha": git("rev-parse", "HEAD"),
        "base_sha": BASE_SHA,
        "venvs": {},
    }
    for (location, code), python in PYTHONS.items():
        cmd = [python, "-c", "import k8s_agent_sandbox, importlib.metadata as m; print(k8s_agent_sandbox.__file__, m.version('k8s-agent-sandbox'), m.version('kubernetes'), m.version('kubernetes_asyncio'))"]
        if location == "pod":
            cmd = ["kubectl", "exec", "-n", DRIVER_NS, DRIVER_POD, "--", *cmd]
        env["venvs"][f"{location}-{code}"] = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    if old is not None:
        same = ("kubernetes", "node_allocatable_pods", "node_pod_cidr", "controller_args", "controller_image",
                "controller_image_id", "venvs")
        if all(old.get(k, env[k]) == env[k] for k in same):
            # Unchanged; keep what was recorded and add only the fields it lacks.
            env = {**env, **old}
        else:
            os.rename(path, os.path.join(RESULTS_DIR, f"env-{time.strftime('%Y%m%dT%H%M%S')}.json"))
    with open(path, "w") as f:
        json.dump(env, f, indent=1)


def jobs(plan):
    """(entry, k) in run order: a run of entries sharing an ``interleave`` key goes run 1 of each,
    then run 2 of each, and so on; any other entry runs its runs back to back."""
    out, block = [], []

    def flush():
        for k in range(1, max((e[3] for e in block), default=0) + 1):
            out.extend((e, k) for e in block if k <= e[3])
        block.clear()

    for entry in plan:
        key = entry[4].get("interleave")
        if block and (key is None or key != block[0][4].get("interleave")):
            flush()
        if key is None:
            out.extend((entry, k) for k in range(1, entry[3] + 1))
        else:
            block.append(entry)
    flush()
    return out


def cmd_suite(args):
    check_context()
    os.makedirs(LOGS_DIR, exist_ok=True)
    tip_branch_sha = check_code()
    record_env()
    statuses = statuses_from_disk()
    write_status(statuses)
    suite_log = open(os.path.join(LOGS_DIR, "suite.log"), "a")

    def log(msg):
        suite_log.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        suite_log.flush()

    # In each result too, since env.json keeps its first value when the cluster is unchanged.
    bench_head = git("rev-parse", "HEAD")
    bench_dirty = bool(git("status", "--porcelain", "--", HERE + "/*.py", HERE + "/*.sh"))
    plan = run_plan()
    for (scenario, variant, shell, runs, spec), k in jobs(plan):
        label = run_label(scenario, variant, shell)
        if args.only and not any(label.startswith(o) for o in args.only):
            continue
        path = result_path(label, k)
        key = f"{label}-run{k}"
        if os.path.exists(path):
            continue
        blocked = run_condition(spec)
        if blocked:
            statuses[key] = blocked
            write_status(statuses)
            continue
        try:
            warm_total = prepare_pools(spec["extras"], log)
            wait_clean(warm_total, log)
        except Exception as e:
            statuses[key] = f"failed in setup: {e}"
            write_status(statuses)
            log(f"{key} setup failed: {e}")
            continue
        statuses[key] = f"running since {time.strftime('%H:%M:%S')}"
        write_status(statuses)
        log(f"{key} start")
        run_spec = {k2: v for k2, v in spec.items() if k2 != "extras"}
        with open(os.path.join(LOGS_DIR, f"{key}.log"), "w") as driver_log:
            try:
                result = run_driver(run_spec, driver_log)
            except Exception as e:
                statuses[key] = f"failed: {e}"
                write_status(statuses)
                log(f"{key} failed: {e}")
                continue
        result.update({"scenario": scenario, "variant": variant, "shell": shell, "run": k, "spec": run_spec,
                       "driver_location": spec["location"], "code": spec["code"],
                       "code_sha": spec.get("code_sha") or (BASE_SHA if spec["code"] == "base" else CODE_SHA),
                       "code_tree": (git("rev-parse", f"{spec['code_sha']}^{{tree}}") if spec.get("code_sha")
                                     else BASE_TREE if spec["code"] == "base" else CODE_TREE),
                       "tip_branch_sha": tip_branch_sha, "bench_head_sha": bench_head, "bench_dirty": bench_dirty,
                       **controller_image(),
                       "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        # Written once, never overwritten: a finished run is one whose file exists.
        with open(path, "x") as f:
            json.dump(result, f, indent=1)
        # Failures of this suite left no file, so they're kept from before.
        statuses = statuses_from_disk() | {k2: v for k2, v in statuses.items() if v.startswith("failed")}
        write_status(statuses)
        log(f"{key} {statuses[key]}")
    log("suite finished")


def cmd_errored(args):
    """Lists runs that finished with errors. With --supersede, moves each first attempt to
    superseded/ with its reason in superseded/WHY.md, so the next suite reruns it once; a run
    whose earlier attempt is already there is kept and flagged."""
    gone = superseded_names()
    sup = os.path.join(RESULTS_DIR, "superseded")
    # Only the 2026-10 rerun's runs; the earlier ones stand as they are, errors included.
    prefixes = tuple(p for ps in STEPS.values() for p in ps)
    for path in sorted(glob.glob(os.path.join(RESULTS_DIR, "*-run*.json"))):
        if not os.path.basename(path).startswith(prefixes):
            continue
        with open(path) as f:
            r = json.load(f)
        why = run_errors(r)
        if not why:
            continue
        name = os.path.basename(path)
        samples = (r.get("errors") or r.get("rollout_error_samples") or r.get("group_errors") or [None])[0]
        line = f"{name}: {why}" + (f"; first error: {str(samples)[:160]}" if samples else "")
        if name in gone:
            print(f"SECOND ATTEMPT FAILED, kept: {line}")
        elif args.supersede:
            os.makedirs(sup, exist_ok=True)
            os.rename(path, os.path.join(sup, name))
            with open(os.path.join(sup, "WHY.md"), "a") as f:
                f.write(f"- {line} (moved {time.strftime('%Y-%m-%d %H:%M')}, rerun once)\n")
            print(f"superseded: {line}")
        else:
            print(f"errored: {line}")



LEGACY = {
    "fill-n1000-run1.json": ("1a", "n1000", 1), "fill-n1000-run2.json": ("1a", "n1000", 2),
    "fill-n1000-run3.json": ("1a", "n1000", 3), "fill-n5000-run1.json": ("1a", "n5000", 1),
    "fill-n5000-run2.json": ("1a", "n5000", 2),
}


def cmd_import_legacy(_args):
    """Copies the earlier 1a runs (defaults, pool_size 22, clean namespace) into the new layout."""
    legacy_dir = os.path.join(RESULTS_DIR, "legacy")
    for name, (scenario, variant, k) in LEGACY.items():
        src = os.path.join(legacy_dir, name)
        dest = result_path(run_label(scenario, variant, "sync"), k)
        if not os.path.exists(src) or os.path.exists(dest):
            continue
        with open(src) as f:
            old = json.load(f)
        new = {
            "imported": True, "imported_from": f"legacy/{name}",
            "scenario": scenario, "variant": variant, "shell": "sync", "run": k,
            "driver_location": "host", "code": "tip", "code_sha": tip_sha(),
            # The earlier suite passed create_rps, max_in_flight and pool_size explicitly; the
            # Lease, quorum and work-budget settings were left at the SDK defaults.
            "settings": {"create_rps": old.get("create_rps"), "max_in_flight": old.get("max_in_flight"),
                         "pool_size": old.get("pool_size"), "lease_duration": 60, "quorum_timeout": 600,
                         "work_budget": 3600},
            "effective_pool_size": old.get("effective_pool_size"),
            "fill_cpu_cores": old.get("fill_cpu_fraction"),
            "release_deletecollection_fraction": (old["release_deletecollection_s"] / old["release_s"]) if old.get("release_s") else None,
            "renew_interval_s": 20,
            "apiserver": {"total": {"429s": (old.get("apiserver", {}).get("total") or {}).get("429s"),
                                    "flowcontrol_rejected": (old.get("apiserver", {}).get("total") or {}).get("flowcontrol_rejected")}},
        }
        for key in ("claim_batch_return_s", "fill_sent_s", "fill_acked_s", "fill_wall_s", "fill_cpu_s", "creates_per_s",
                    "create_attempts", "creates_ok", "creates_retried", "create_429s", "create_p50_ms", "create_p99_ms",
                    "create_max_ms", "create_peak_in_flight", "rss_peak_mb", "ru_maxrss_mb", "api_conns_peak",
                    "pool_discard_warnings", "lease_writes", "renewal_gap_max_s", "lease_degraded_ever", "members_ms",
                    "members_count", "detach_s", "attach_s", "attach_rss_delta_mb", "attach_list_s", "release_s",
                    "release_rounds", "release_deletecollection_s", "release_list_s", "release_list_max_s",
                    "release_list_fraction", "release_rss_delta_mb", "driver_exit", "batch_err"):
            new[key] = old.get(key)
        with open(dest, "x") as f:
            json.dump(new, f, indent=1)
        print(f"imported {name} -> {os.path.basename(dest)}")


def cmd_setup(_args):
    check_context()
    kubectl("apply", "-f", "-", input=SETUP_MANIFEST)
    # The earlier setup bound cluster-admin; the README's Role replaces it.
    kubectl("delete", "clusterrolebinding", "bench-driver", "--ignore-not-found", check=False)
    kubectl("wait", "-n", DRIVER_NS, "--for=condition=Ready", f"pod/{DRIVER_POD}", "--timeout=300s")
    print(kubectl("exec", "-n", DRIVER_NS, DRIVER_POD, "--", "bash", f"{POD_REPO_ROOT}/test/benchmarks/batch-claim/setup-venvs.sh", "pod"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("import-legacy")
    suite = sub.add_parser("suite")
    suite.add_argument("--only", nargs="*", help="run-label prefixes, such as '3a' or '1b-n5000-rps20'")
    sub.add_parser("report")
    sub.add_parser("candidate")
    errored = sub.add_parser("errored")
    errored.add_argument("--supersede", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("spec")
    args = parser.parse_args()
    if args.cmd == "run":
        Driver(json.loads(args.spec)).run()
    elif args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "import-legacy":
        cmd_import_legacy(args)
    elif args.cmd == "suite":
        cmd_suite(args)
    elif args.cmd == "errored":
        cmd_errored(args)
    elif args.cmd == "candidate":
        import report

        report.write_candidate(RESULTS_DIR)
    else:
        import report

        report.main(RESULTS_DIR, os.path.join(HERE, "bench-report.md"))


if __name__ == "__main__":
    main()
