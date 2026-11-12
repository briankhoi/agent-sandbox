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

"""Multi-cluster wiring.

A `Cluster` owns its own kube `ApiClient` (so we can target many contexts at
once) and exposes per-cluster `CustomObjectsApi`/`CoreV1Api`/`AppsV1Api`, a
`Resources` manager, and — pointed at this context via attribute injection — the
SDK's `K8sHelper` / `SandboxClient` (built lazily). No SDK fork required.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from collections import OrderedDict
from urllib.parse import urlencode

from kubernetes import client
from kubernetes import config as k8s_config

from .config import ClusterConfig, TemplateSpec
from .resources import Resources


async def build_async_api_client(cfg: ClusterConfig, *, pool_maxsize: int = 1000):
  """Build a ``kubernetes_asyncio.client.ApiClient`` for one cluster's context."""
  from kubernetes_asyncio import client as async_client
  from kubernetes_asyncio import config as k8s_async_config

  configuration = async_client.Configuration()
  configuration.connection_pool_maxsize = pool_maxsize
  if cfg.in_cluster:
    k8s_async_config.load_incluster_config(client_configuration=configuration)
  else:
    await k8s_async_config.load_kube_config(
        config_file=cfg.kubeconfig,
        context=cfg.context,
        client_configuration=configuration,
    )
  return async_client.ApiClient(configuration=configuration)


async def _build_fast_ws_core_api(
    cfg: ClusterConfig,
    *,
    pool_maxsize: int = 1000,
    base_configuration=None,
):
  """Build a `kubernetes_asyncio` `CoreV1Api` backed by a lightweight `WsApiClient`."""
  import aiohttp
  from kubernetes_asyncio import client as async_client
  from kubernetes_asyncio import config as k8s_async_config
  from kubernetes_asyncio.stream import WsApiClient
  from kubernetes_asyncio.stream.ws_client import (
      STDERR_CHANNEL,
      STDOUT_CHANNEL,
      WsResponse,
      get_websocket_url,
  )

  class _FastWsApiClient(WsApiClient):
    def __init__(self, configuration) -> None:
      super().__init__(configuration=configuration)
      # Replace default 2 MiB read_bufsize ClientSession with a 64 KiB session
      # sharing the same SSLContext so 500-1000 concurrent execs stay compact.
      old_pm = self.rest_client.pool_manager
      connector = old_pm.connector
      self.rest_client.pool_manager = aiohttp.ClientSession(
          connector=connector,
          connector_owner=True,
          trust_env=True,
          read_bufsize=65536,
      )
      old_pm._connector_owner = False
      # Old session has made 0 requests; detach cleanly without closing connector.
      self._detached_old_pm = old_pm

    async def close(self) -> None:
      old_pm = getattr(self, "_detached_old_pm", None)
      if old_pm is not None:
        self._detached_old_pm = None
        try:
          await old_pm.close()
        except Exception:  # noqa: BLE001
          pass
      await super().close()

    async def request(
        self,
        method,
        url,
        query_params=None,
        headers=None,
        post_params=None,
        body=None,
        _preload_content=True,
        _request_timeout=None,
    ):
      if query_params:
        expanded = []
        for key, value in query_params:
          if key == "command" and isinstance(value, list):
            for cmd_part in value:
              expanded.append((key, cmd_part))
          else:
            expanded.append((key, value))
        query_params = expanded
      hdrs = dict(headers) if headers else {}
      hdrs.setdefault("sec-websocket-protocol", "v4.channel.k8s.io")
      if query_params:
        url = f"{url}?{urlencode(query_params)}"
      ws_url = get_websocket_url(url)
      ws_kwargs = {
          "headers": hdrs,
          "heartbeat": self.heartbeat,
          "proxy": self.rest_client.proxy,
          "proxy_headers": self.rest_client.proxy_headers,
      }
      server_hostname = getattr(self.rest_client, "server_hostname", None)
      if server_hostname:
        ws_kwargs["server_hostname"] = server_hostname
      if not _preload_content:
        return self.rest_client.pool_manager.ws_connect(ws_url, **ws_kwargs)
      buf = bytearray()
      try:
        async with self.rest_client.pool_manager.ws_connect(
            ws_url, **ws_kwargs
        ) as ws:
          async for wsmsg in ws:
            raw = wsmsg.data
            if isinstance(raw, bytes):
              if len(raw) > 1 and raw[0] in (STDOUT_CHANNEL, STDERR_CHANNEL):
                buf.extend(raw[1:])
            elif isinstance(raw, str):
              if len(raw) > 1 and ord(raw[0]) in (STDOUT_CHANNEL, STDERR_CHANNEL):
                buf.extend(raw[1:].encode("utf-8"))
      except aiohttp.WSServerHandshakeError as exc:
        raise async_client.ApiException(status=exc.status, reason=exc.message) from exc
      return WsResponse(200, bytes(buf))

  if base_configuration is not None:
    configuration = copy.copy(base_configuration)
    configuration.connection_pool_maxsize = pool_maxsize
  else:
    configuration = async_client.Configuration()
    configuration.connection_pool_maxsize = pool_maxsize
    if cfg.in_cluster:
      k8s_async_config.load_incluster_config(client_configuration=configuration)
    else:
      await k8s_async_config.load_kube_config(
          config_file=cfg.kubeconfig,
          context=cfg.context,
          client_configuration=configuration,
      )
  ws_api = _FastWsApiClient(configuration)
  return ws_api, async_client.CoreV1Api(ws_api)


def build_api_client(cfg: ClusterConfig, *, pool_maxsize: int = 1000):
  """Build a kube ApiClient for one cluster's context (mockable in tests).

  Sizes the urllib3 connection pool to ``pool_maxsize`` (default 1000). The
  kubernetes client default (~60) throttles high-concurrency claim fan-out: a
  500–1000-wide ``process_parallel`` otherwise serializes on the pool ("Connection
  pool is full, discarding connection"), inflating claim latency and stalling large
  runs. Sizing it to the concurrency budget keeps claims parallel."""
  configuration = client.Configuration()
  configuration.connection_pool_maxsize = pool_maxsize
  if cfg.in_cluster:
    k8s_config.load_incluster_config(client_configuration=configuration)
  else:
    k8s_config.load_kube_config(config_file=cfg.kubeconfig, context=cfg.context,
                                client_configuration=configuration)
  return client.ApiClient(configuration)


def _resize_async_api_client_pool(api_client, needed: int) -> None:
  if api_client is None:
    return
  cfg_obj = getattr(api_client, "configuration", None)
  cur = getattr(cfg_obj, "connection_pool_maxsize", None)
  conn = getattr(
      getattr(getattr(api_client, "rest_client", None), "pool_manager", None),
      "connector",
      None,
  )
  conn_limit = getattr(conn, "_limit", None)
  if (
      conn is not None
      and isinstance(conn_limit, int)
      and not isinstance(conn_limit, bool)
      and 0 < conn_limit < needed
  ):
    conn._limit = needed
    if cfg_obj is not None and isinstance(cur, int) and not isinstance(cur, bool) and 0 < cur < needed:
      cfg_obj.connection_pool_maxsize = needed
  elif conn is None and cfg_obj is not None and isinstance(cur, int) and not isinstance(cur, bool) and 0 < cur < needed:
    cfg_obj.connection_pool_maxsize = needed


def _resize_api_client_pool(api_client, needed: int) -> None:
  cfg_obj = getattr(api_client, "configuration", None)
  cur = getattr(cfg_obj, "connection_pool_maxsize", None)
  if cfg_obj is None or not (
      isinstance(cur, int) and not isinstance(cur, bool) and 0 < cur < needed
  ):
    return
  cfg_obj.connection_pool_maxsize = needed
  rest = getattr(api_client, "rest_client", None)
  pm = getattr(rest, "pool_manager", None)
  pool_kw = getattr(pm, "connection_pool_kw", None)
  if isinstance(pool_kw, dict):
    pool_kw["maxsize"] = needed
    clear_fn = getattr(pm, "clear", None)
    if pm is not None and type(pm).__module__.startswith("urllib3") and callable(clear_fn):
      clear_fn()


class Cluster:
  """A single target cluster and its API clients."""

  def __init__(self, config: ClusterConfig, *, api_client=None,
               async_api_client=None,
               labels: dict | None = None, pool_maxsize: int = 1000):
    self.config = config
    self.name = config.name
    self.namespace = config.namespace
    self._injected_sync_api_client = api_client is not None
    self._injected_async_api_client = async_api_client is not None
    self._pool_maxsize = pool_maxsize
    if api_client is not None:
      self.api_client = api_client
    elif pool_maxsize == 1000:
      self.api_client = build_api_client(config)
    else:
      try:
        self.api_client = build_api_client(config, pool_maxsize=pool_maxsize)
      except TypeError:
        self.api_client = build_api_client(config)
    self.custom_api = client.CustomObjectsApi(self.api_client)
    self.core_api = client.CoreV1Api(self.api_client)
    self.apps_api = client.AppsV1Api(self.api_client)
    self.coordination_api = client.CoordinationV1Api(self.api_client)
    self.resources = Resources(
        self.custom_api, self.core_api, self.namespace, labels=labels,
        coordination_api=self.coordination_api)
    self._k8s_helper = None
    self._sandbox_client = None
    self._async_api_client = async_api_client
    self._async_k8s_helper = None
    self._async_sandbox_client = None
    self._async_ws_api_client = None
    self._async_exec_core_api = None
    self._async_loop: asyncio.AbstractEventLoop | None = None
    self._async_init_lock: asyncio.Lock | None = None
    self._async_state_lock = threading.Lock()
    # Concurrent first acquires would otherwise each build a SandboxClient,
    # and every build loads the kubeconfig.
    self._sandbox_client_lock = threading.Lock()
    # Separate from _sandbox_client_lock, which is held while sandbox_client reads
    # k8s_helper, because threading.Lock is not reentrant.
    self._k8s_helper_lock = threading.Lock()
    self.tracer_config = None                 # optional SDK SandboxTracerConfig
    # Placement / capacity bookkeeping. Mutated from parallel claim workers, so
    # guard with a per-cluster lock and only touch via the methods below.
    self._count_lock = threading.Lock()
    self.active_replicas = 0
    self.active_claims = 0
    # One CoreV1Api per thread for router-free pod exec (see exec_core_api).
    self._exec_local = threading.local()
    self.ensure_pool_maxsize(pool_maxsize)

  @property
  def supports_native_async_batch(self) -> bool:
    """Whether ``AsyncSandboxFleet`` can drive ``AsyncSandboxBatch`` on this cluster.

    Disabled when ``ClusterConfig(snapshots=True)`` (the PodSnapshot extension
    is sync-only) or when a test injected a synchronous ``api_client`` mock
    without an ``async_api_client``.
    """
    if getattr(self.config, "snapshots", False):
      return False
    if self._injected_sync_api_client and not self._injected_async_api_client:
      return False
    try:
      import kubernetes_asyncio  # noqa: F401
    except ImportError:
      return False
    return True

  def ensure_pool_maxsize(self, needed: int) -> None:
    """Grow ``api_client``'s urllib3 connection pool to at least ``needed``."""
    if needed > self._pool_maxsize:
      self._pool_maxsize = needed
    _resize_api_client_pool(self.api_client, needed)
    helper = self._k8s_helper
    if helper is not None:
      for attr in ("coordination_v1_api", "_batch_custom_objects_api"):
        sub_api = getattr(helper, attr, None)
        sub_client = getattr(sub_api, "api_client", None)
        if sub_client is not None:
          _resize_api_client_pool(sub_client, needed)
    _resize_async_api_client_pool(self._async_api_client, needed)
    _resize_async_api_client_pool(self._async_ws_api_client, needed)

  def exec_core_api(self):
    """A thread-local `CoreV1Api` for router-free pod exec.

    The kubernetes ``stream()`` (websocket) exec is not thread-safe across a
    shared client, so each thread gets its own client — but cached per thread
    rather than rebuilt per call. A fresh ``ApiClient`` per exec would leak a
    urllib3 connection pool + thread pool every time (GC-reliant), which at this
    package's scale (a sandbox per trajectory → many execs) exhausts fds.
    """
    core = getattr(self._exec_local, "core_api", None)
    if core is None:
      core = client.CoreV1Api(client.ApiClient(self.api_client.configuration))
      self._exec_local.core_api = core
    return core

  @property
  def k8s_helper(self):
    """SDK K8sHelper pointed at this cluster's context (lazy, injected)."""
    if self._k8s_helper is not None:
      return self._k8s_helper
    with self._k8s_helper_lock:
      if self._k8s_helper is None:
        from k8s_agent_sandbox.k8s_helper import K8sHelper
        # With an ApiClient the helper loads no kubeconfig, so the process-wide
        # default Configuration stays untouched.
        helper = K8sHelper(api_client=self.api_client)
        helper.custom_objects_api = self.custom_api
        helper.core_v1_api = self.core_api
        self._k8s_helper = helper
      return self._k8s_helper

  @property
  def sandbox_client(self):
    """SDK SandboxClient using this cluster's K8sHelper (lazy, injected).

    With ``ClusterConfig(snapshots=True)`` this is the SDK's GKE PodSnapshot-
    capable client instead (same claim API; its sandboxes add snapshot /
    suspend / resume / restore — see `agent_sandbox_rl.snapshots`), bound to
    this cluster's helper *before* its CRD check runs.
    """
    if self._sandbox_client is not None:
      return self._sandbox_client
    with self._sandbox_client_lock:
      if self._sandbox_client is None:
        if getattr(self.config, "snapshots", False):
          from .snapshots import build_snapshot_client
          c = build_snapshot_client(self.k8s_helper, tracer_config=self.tracer_config,
                                    api_client=self.api_client)
        else:
          from k8s_agent_sandbox import SandboxClient
          # The api_client keeps the helper this builds from loading a kubeconfig,
          # since it is replaced right after.
          c = (SandboxClient(tracer_config=self.tracer_config, api_client=self.api_client)
               if self.tracer_config is not None
               else SandboxClient(api_client=self.api_client))
          c.k8s_helper = self.k8s_helper
        self._sandbox_client = c
      return self._sandbox_client

  async def _ensure_async_loop(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
    """Ensure cached async clients belong to ``loop``, resetting stale loop state atomically."""
    with self._async_state_lock:
      if self._async_loop is loop and self._async_init_lock is not None:
        return self._async_init_lock
      if self._async_loop is None:
        init_lock = self._async_init_lock or asyncio.Lock()
        self._async_init_lock = init_lock
        self._async_loop = loop
        return init_lock
      old_loop = self._async_loop
      old_sb = self._async_sandbox_client
      old_api = None if self._injected_async_api_client else self._async_api_client
      old_ws = self._async_ws_api_client
      self._async_sandbox_client = None
      self._async_k8s_helper = None
      self._async_ws_api_client = None
      self._async_exec_core_api = None
      if not self._injected_async_api_client:
        self._async_api_client = None
      init_lock = asyncio.Lock()
      self._async_init_lock = init_lock
      self._async_loop = loop

    if old_loop is not None and not old_loop.is_closed():
      for obj in (old_sb, old_api, old_ws):
        if obj is None:
          continue
        close_fn = getattr(obj, "close", None)
        if not callable(close_fn):
          continue
        try:
          if old_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(close_fn(), old_loop)
            fut.add_done_callback(lambda d: None if d.cancelled() else d.exception())
          else:
            res = close_fn()
            if asyncio.iscoroutine(res):
              res.close()
        except Exception:  # noqa: BLE001
          pass
    return init_lock

  async def get_async_sandbox_client(self):
    """SDK ``AsyncSandboxClient`` bound to this cluster's async API client."""
    loop = asyncio.get_running_loop()
    init_lock = await self._ensure_async_loop(loop)
    if self._async_sandbox_client is not None:
      return self._async_sandbox_client

    async with init_lock:
      if self._async_sandbox_client is not None:
        return self._async_sandbox_client
      if self._async_api_client is None:
        self._async_api_client = await build_async_api_client(
            self.config, pool_maxsize=self._pool_maxsize
        )
      else:
        _resize_async_api_client_pool(self._async_api_client, self._pool_maxsize)
      from k8s_agent_sandbox import AsyncSandboxClient
      from k8s_agent_sandbox.models import SandboxInClusterConnectionConfig

      kwargs = {}
      if self.tracer_config is not None:
        kwargs["tracer_config"] = self.tracer_config
      # SandboxFleet owns teardown and atexit hooks; disable duplicate client atexit registration.
      c = AsyncSandboxClient(
          connection_config=SandboxInClusterConnectionConfig(),
          cleanup=False,
          api_client=self._async_api_client,
          **kwargs,
      )
      await c.k8s_helper._ensure_initialized()
      self._async_k8s_helper = c.k8s_helper
      self._async_sandbox_client = c
      return self._async_sandbox_client

  async def async_exec_core_api(self):
    """Shared event-loop-native ``CoreV1Api`` over ``WsApiClient`` for ``aexec()``."""
    loop = asyncio.get_running_loop()
    init_lock = await self._ensure_async_loop(loop)
    if self._async_exec_core_api is not None:
      return self._async_exec_core_api

    async with init_lock:
      if self._async_exec_core_api is not None:
        return self._async_exec_core_api
      from kubernetes_asyncio import client as async_client
      raw_cfg = (
          getattr(self._async_api_client, "configuration", None)
          if self._injected_async_api_client and self._async_api_client is not None
          else None
      )
      base_cfg = raw_cfg if isinstance(raw_cfg, async_client.Configuration) else None
      ws_api, core_api = await _build_fast_ws_core_api(
          self.config,
          pool_maxsize=self._pool_maxsize,
          base_configuration=base_cfg,
      )
      self._async_ws_api_client = ws_api
      self._async_exec_core_api = core_api
      return self._async_exec_core_api

  async def aclose(self) -> None:
    """Close any owned ``kubernetes_asyncio`` ``ApiClient`` and ``WsApiClient`` sessions."""
    with self._async_state_lock:
      sb_client = self._async_sandbox_client
      api_client = self._async_api_client
      ws_api = self._async_ws_api_client
      self._async_sandbox_client = None
      self._async_k8s_helper = None
      self._async_ws_api_client = None
      self._async_exec_core_api = None
      if not self._injected_async_api_client:
        self._async_api_client = None
    if sb_client is not None:
      close_fn = getattr(sb_client, "close", None)
      if callable(close_fn):
        try:
          await close_fn()
        except Exception:  # noqa: BLE001
          pass
    if api_client is not None and not self._injected_async_api_client:
      close_api = getattr(api_client, "close", None)
      if callable(close_api):
        try:
          await close_api()
        except Exception:  # noqa: BLE001
          pass
    if ws_api is not None:
      close_ws = getattr(ws_api, "close", None)
      if callable(close_ws):
        try:
          await close_ws()
        except Exception:  # noqa: BLE001
          pass

  def template_spec(self, base: TemplateSpec) -> TemplateSpec:
    """Merge per-cluster overrides onto a base TemplateSpec."""
    data = base.model_dump()
    if self.config.node_selector is not None:
      data["node_selector"] = self.config.node_selector
    if self.config.runtime_class is not None:
      data["runtime_class"] = self.config.runtime_class
    if self.config.image_pull_secret is not None:
      data["image_pull_secret"] = self.config.image_pull_secret
    return TemplateSpec(**data)

  @property
  def capacity(self) -> int | None:
    return self.config.max_replicas

  def has_capacity(self, additional: int) -> bool:
    with self._count_lock:
      if self.config.max_replicas is None:
        return True
      return self.active_replicas + additional <= self.config.max_replicas

  # --- atomic capacity bookkeeping (thread-safe) ------------------------- #
  def reserve_replicas(self, n: int) -> None:
    with self._count_lock:
      self.active_replicas += n

  def release_replicas(self, n: int) -> None:
    with self._count_lock:
      self.active_replicas = max(0, self.active_replicas - n)

  def reserve_claim(self) -> None:
    with self._count_lock:
      self.active_claims += 1

  def release_claim(self) -> None:
    with self._count_lock:
      if self.active_claims > 0:
        self.active_claims -= 1

  def reset_counts(self) -> None:
    with self._count_lock:
      self.active_replicas = 0
      self.active_claims = 0

  def __repr__(self) -> str:
    return (f"Cluster(name={self.name!r}, namespace={self.namespace!r}, "
            f"active_replicas={self.active_replicas}, "
            f"active_claims={self.active_claims})")


class ClusterRegistry:
  """An ordered set of clusters, keyed by name."""

  def __init__(self, clusters: list[Cluster] | None = None):
    self._by_name: "OrderedDict[str, Cluster]" = OrderedDict()
    for c in clusters or []:
      self.add(c)

  def add(self, cluster: Cluster) -> None:
    if cluster.name in self._by_name:
      raise ValueError(f"duplicate cluster name: {cluster.name!r}")
    self._by_name[cluster.name] = cluster

  def get(self, name: str) -> Cluster:
    return self._by_name[name]

  def names(self) -> list[str]:
    return list(self._by_name.keys())

  def __iter__(self):
    return iter(self._by_name.values())

  def __len__(self) -> int:
    return len(self._by_name)

  @classmethod
  def from_configs(cls, configs: list[ClusterConfig],
                   *, labels: dict | None = None,
                   pool_maxsize: int = 1000) -> "ClusterRegistry":
    return cls([
        Cluster(c, labels=labels, pool_maxsize=pool_maxsize) for c in configs
    ])
