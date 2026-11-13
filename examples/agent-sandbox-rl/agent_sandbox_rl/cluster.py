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
import contextlib
import copy
import inspect
import logging
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from kubernetes import client
from kubernetes import config as k8s_config

from . import constants
from .config import ClusterConfig, TemplateSpec
from .resources import Resources

logger = logging.getLogger(__name__)

# Background deletes of released sandboxes per cluster: enough to keep up with a few
# hundred releases a second without adding much API-server load.
RELEASED_SANDBOX_DELETE_WORKERS = 16
RELEASED_SANDBOX_DELETE_TIMEOUT = (3, 10)
# More than the workers, so one stalled moment with every worker mid-request does
# not look like a lost connection.
RELEASED_SANDBOX_MAX_ERRORS = 2 * RELEASED_SANDBOX_DELETE_WORKERS
RELEASED_SANDBOX_PAUSE_SECONDS = 30.0


def _env_configures_proxy_or_netrc() -> bool:
  """Whether this process has a proxy or netrc that aiohttp's ``trust_env`` would apply."""
  try:
    if urllib.request.getproxies():
      return True
  except Exception:  # noqa: BLE001 - when unsure, keep aiohttp's default behaviour
    return True
  if os.environ.get("NETRC"):
    return True
  home = os.path.expanduser("~")
  return any(os.path.isfile(os.path.join(home, n)) for n in (".netrc", "_netrc"))


def _skip_unused_trust_env(session) -> None:
  """Stop ``session`` consulting proxy env vars and netrc on every request when neither exists.

  With ``trust_env`` aiohttp hands the netrc lookup and the proxy lookup of each request to the
  loop's thread pool, which costs two thread hops per claim create, delete and exec.

  The environment is read once, when the client is built: a proxy variable or netrc file added
  later is not picked up until the client is rebuilt.
  """
  if getattr(session, "_trust_env", False) is True and not _env_configures_proxy_or_netrc():
    session._trust_env = False


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
  api = async_client.ApiClient(configuration=configuration)
  session = getattr(getattr(api, "rest_client", None), "pool_manager", None)
  _skip_unused_trust_env(session)
  conn = getattr(session, "connector", None)
  ka = getattr(conn, "_keepalive_timeout", None)
  if isinstance(ka, (int, float)) and not isinstance(ka, bool) and ka < 180.0:
    conn._keepalive_timeout = 180.0
  return api


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
      ka = getattr(connector, "_keepalive_timeout", None)
      if isinstance(ka, (int, float)) and not isinstance(ka, bool) and ka < 180.0:
        connector._keepalive_timeout = 180.0
      self.rest_client.pool_manager = aiohttp.ClientSession(
          connector=connector,
          connector_owner=True,
          trust_env=_env_configures_proxy_or_netrc(),
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
            msg_type = getattr(wsmsg, "type", None)
            raw = wsmsg.data
            if msg_type == aiohttp.WSMsgType.ERROR:
              ws_exc = ws.exception() or (
                  raw
                  if isinstance(raw, BaseException)
                  else aiohttp.WebSocketError(
                      aiohttp.WSCloseCode.ABNORMAL_CLOSURE,
                      "websocket exec stream error",
                  )
              )
              raise ws_exc
            if isinstance(raw, bytes):
              if len(raw) > 1 and raw[0] in (STDOUT_CHANNEL, STDERR_CHANNEL):
                buf.extend(raw[1:])
            elif isinstance(raw, str):
              if len(raw) > 1 and ord(raw[0]) in (STDOUT_CHANNEL, STDERR_CHANNEL):
                buf.extend(raw[1:].encode("utf-8"))
      except aiohttp.WSServerHandshakeError as exc:
        err = async_client.ApiException(status=exc.status, reason=exc.message)
        err.body = b""
        raise err from exc
      try:
        buf.decode("utf-8")
        payload = bytes(buf)
      except UnicodeDecodeError:
        payload = buf.decode("utf-8", errors="replace").encode("utf-8")
      return WsResponse(200, payload)

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


def _api_session_closed(api_obj) -> bool:
  if api_obj is None:
    return False
  pm = getattr(getattr(api_obj, "rest_client", None), "pool_manager", None)
  return getattr(pm, "closed", False) is True


def _force_close_transport_offline(tr) -> None:
  if tr is None:
    return
  ssl_proto = getattr(tr, "_ssl_protocol", None)
  raw_tr = getattr(ssl_proto, "_transport", None) if ssl_proto is not None else None
  for t_obj in (tr, raw_tr):
    if t_obj is None:
      continue
    t_loop = getattr(t_obj, "_loop", None)
    sock = getattr(t_obj, "_sock", None)
    sock_fd = getattr(t_obj, "_sock_fd", None)
    if sock_fd is None and sock is not None:
      with contextlib.suppress(Exception):
        sock_fd = sock.fileno()
    if (
        t_loop is not None
        and not t_loop.is_closed()
        and isinstance(sock_fd, int)
        and sock_fd >= 0
    ):
      for rm_name in ("_remove_reader", "_remove_writer"):
        rm_fn = getattr(t_loop, rm_name, None)
        if callable(rm_fn):
          with contextlib.suppress(Exception):
            rm_fn(sock_fd)
    if sock is not None:
      with contextlib.suppress(Exception):
        sock.close()
    for attr, val in (("_sock", None), ("_closing", True), ("_closed", True)):
      if hasattr(t_obj, attr):
        with contextlib.suppress(Exception):
          setattr(t_obj, attr, val)
  if ssl_proto is not None and hasattr(ssl_proto, "_transport"):
    with contextlib.suppress(Exception):
      ssl_proto._transport = None


def _close_k8s_async_client_offline(obj) -> None:
  if obj is None:
    return
  old_pm = getattr(obj, "_detached_old_pm", None)
  if old_pm is not None:
    with contextlib.suppress(Exception):
      obj._detached_old_pm = None
  pm = getattr(getattr(obj, "rest_client", None), "pool_manager", None)
  for sess in (old_pm, pm):
    if sess is None or not type(sess).__module__.startswith("aiohttp"):
      continue
    conn = getattr(sess, "_connector", None)
    if conn is not None and getattr(sess, "_connector_owner", False):
      transports = []
      conns_map = getattr(conn, "_conns", None)
      if isinstance(conns_map, dict):
        for entries in list(conns_map.values()):
          for proto, _ in list(entries):
            transports.append(getattr(proto, "transport", None))
        conns_map.clear()
      acquired_set = getattr(conn, "_acquired", None)
      if isinstance(acquired_set, (set, list)):
        for proto in list(acquired_set):
          transports.append(getattr(proto, "transport", None))
        acquired_set.clear()
      acq_per_host = getattr(conn, "_acquired_per_host", None)
      if hasattr(acq_per_host, "clear"):
        acq_per_host.clear()
      closed_trs = getattr(conn, "_cleanup_closed_transports", None)
      if isinstance(closed_trs, list):
        transports.extend(closed_trs)
        closed_trs.clear()
      for h_attr in ("_cleanup_handle", "_cleanup_closed_handle"):
        h = getattr(conn, h_attr, None)
        if h is not None and callable(getattr(h, "cancel", None)):
          with contextlib.suppress(Exception):
            h.cancel()
          with contextlib.suppress(Exception):
            setattr(conn, h_attr, None)
      resolve_tasks = getattr(conn, "_resolve_host_tasks", None)
      if isinstance(resolve_tasks, (set, list)):
        for rt in list(resolve_tasks):
          if hasattr(rt, "_log_destroy_pending"):
            with contextlib.suppress(Exception):
              rt._log_destroy_pending = False
          if callable(getattr(rt, "cancel", None)):
            with contextlib.suppress(Exception):
              rt.cancel()
        resolve_tasks.clear()
      for tr in transports:
        _force_close_transport_offline(tr)
      close_sync = getattr(conn, "_close", None)
      if callable(close_sync):
        with contextlib.suppress(Exception):
          close_sync()
    if hasattr(sess, "_connector"):
      with contextlib.suppress(Exception):
        sess._connector = None


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
    self._released_sandbox_lock = threading.Lock()
    self._released_sandbox_pool: ThreadPoolExecutor | None = None
    self._released_sandbox_disabled = False
    self._released_sandbox_errors = 0
    self._released_sandbox_paused_until = 0.0
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

  @staticmethod
  async def _close_async_bundle(
      cur_loop: asyncio.AbstractEventLoop,
      target_loop: asyncio.AbstractEventLoop | None,
      sb_client,
      api_client,
      ws_api,
      *,
      wait_cross_loop: bool,
  ) -> None:
    if target_loop is cur_loop:
      for obj in (sb_client, api_client, ws_api):
        if obj is None:
          continue
        close_fn = getattr(obj, "close", None)
        if not callable(close_fn):
          continue
        try:
          res = close_fn()
          if inspect.isawaitable(res):
            await res
        except Exception:  # noqa: BLE001
          pass
      return

    if (
        target_loop is not None
        and not target_loop.is_closed()
        and target_loop.is_running()
    ):
      for obj in (sb_client, api_client, ws_api):
        if obj is None:
          continue
        close_fn = getattr(obj, "close", None)
        if not callable(close_fn):
          continue
        try:
          res = close_fn()
          if asyncio.iscoroutine(res):
            fut = asyncio.run_coroutine_threadsafe(res, target_loop)
            fut.add_done_callback(
                lambda d: None if d.cancelled() else d.exception()
            )
            if wait_cross_loop:
              with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.wrap_future(fut, loop=cur_loop), timeout=5.0
                )
        except Exception:  # noqa: BLE001
          pass
      return

    # target_loop is None, closed, or stopped: avoid awaiting loop-bound tasks on
    # AsyncSandboxClient while closing all local sandbox handles and aiohttp sessions.
    extra_api = None
    if sb_client is not None:
      if type(sb_client).__name__ == "AsyncSandboxClient":
        tracked = list(getattr(sb_client, "_active_connection_sandboxes", {}).values())
        batches = list(getattr(sb_client, "_active_batches", {}).values()) + list(
            getattr(sb_client, "_closing_batches", ())
        )
        for b in batches:
          b._detached = True
          stop_evt = getattr(b, "_stop_creating", None)
          if stop_evt is not None:
            stop_evt.set()
          if target_loop is not None and not target_loop.is_closed():
            for attr in ("_watch_task", "_renew_task"):
              t = getattr(b, attr, None)
              if t is not None and not t.done():
                if hasattr(t, "_log_destroy_pending"):
                  with contextlib.suppress(Exception):
                    t._log_destroy_pending = False
                with contextlib.suppress(Exception):
                  t.cancel()
          conn_map = getattr(b, "_connected", None)
          if isinstance(conn_map, dict):
            for entry in list(conn_map.values()):
              tracked.append(entry[0] if isinstance(entry, tuple) else entry)
          stale_map = getattr(b, "_stale_connections", None)
          if isinstance(stale_map, (dict, list, set)):
            tracked.extend(list(stale_map))
        for sb in tracked:
          atexit_fn = getattr(type(sb), "_close_for_atexit", None)
          if callable(atexit_fn):
            with contextlib.suppress(Exception):
              atexit_fn(sb)
        helper = getattr(sb_client, "k8s_helper", None)
        if getattr(helper, "_owns_api_client", False):
          extra_api = getattr(helper, "_api_client", None) or getattr(
              getattr(helper, "custom_objects_api", None), "api_client", None
          )
          helper._api_client = None
          helper._initialized = False
      else:
        close_sb = getattr(sb_client, "close", None)
        if callable(close_sb):
          try:
            res = close_sb()
            if inspect.isawaitable(res):
              await res
          except Exception:  # noqa: BLE001
            pass

    for obj in (extra_api, api_client, ws_api):
      if obj is None:
        continue
      _close_k8s_async_client_offline(obj)
      close_fn = getattr(obj, "close", None)
      if not callable(close_fn):
        continue
      try:
        res = close_fn()
        if inspect.isawaitable(res):
          await res
      except Exception:  # noqa: BLE001
        pass

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

    await self._close_async_bundle(
        loop, old_loop, old_sb, old_api, old_ws, wait_cross_loop=False
    )
    return init_lock

  def _async_sb_client_usable(self) -> bool:
    sb = self._async_sandbox_client
    if sb is None:
      return False
    if self._injected_async_api_client:
      return True
    if self._async_api_client is not None and _api_session_closed(self._async_api_client):
      return False
    helper = getattr(sb, "k8s_helper", None)
    h_api = getattr(helper, "_api_client", None) or getattr(
        getattr(helper, "custom_objects_api", None), "api_client", None
    )
    if h_api is not None and _api_session_closed(h_api):
      return False
    return True

  async def get_async_sandbox_client(self):
    """SDK ``AsyncSandboxClient`` bound to this cluster's async API client."""
    loop = asyncio.get_running_loop()
    init_lock = await self._ensure_async_loop(loop)
    if self._async_sb_client_usable():
      return self._async_sandbox_client

    async with init_lock:
      if self._async_sb_client_usable():
        return self._async_sandbox_client
      old_sb = self._async_sandbox_client
      old_api = None
      self._async_sandbox_client = None
      self._async_k8s_helper = None
      if (
          not self._injected_async_api_client
          and self._async_api_client is not None
          and _api_session_closed(self._async_api_client)
      ):
        old_api = self._async_api_client
        self._async_api_client = None
      if old_sb is not None or old_api is not None:
        await self._close_async_bundle(
            loop, loop, old_sb, old_api, None, wait_cross_loop=False
        )
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
    if self._async_exec_core_api is not None and not _api_session_closed(
        self._async_ws_api_client
    ):
      return self._async_exec_core_api

    async with init_lock:
      if self._async_exec_core_api is not None and not _api_session_closed(
          self._async_ws_api_client
      ):
        return self._async_exec_core_api
      old_ws = self._async_ws_api_client
      self._async_ws_api_client = None
      self._async_exec_core_api = None
      if old_ws is not None:
        await self._close_async_bundle(
            loop, loop, None, None, old_ws, wait_cross_loop=False
        )
      from kubernetes_asyncio import client as async_client
      raw_cfg = (
          getattr(self._async_api_client, "configuration", None)
          if self._async_api_client is not None
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

  def delete_released_sandbox(self, sandbox_name: str | None, pod_name: str | None) -> None:
    """Delete a released batch claim's Sandbox and pod now, in the background.

    Deleting a claim leaves its Sandbox, and then the Sandbox's pod, to the cluster's
    garbage collector. On GKE that removed about 13 objects a second, so a fleet that
    releases hundreds of claims per step kept finished pods on the nodes and the warm
    pools' replacement pods waited behind them for minutes. Deleting both directly
    frees the nodes as fast as the fleet releases.

    Call it only after the claim is gone: a live claim whose Sandbox disappears adopts
    another warm sandbox. Best effort: the garbage collector still removes whatever
    this misses. The first 403, on either object, turns it off for this cluster, so
    a driver without delete access does not repeat a failing request for every
    release. `RELEASED_SANDBOX_MAX_ERRORS` requests in a row that never reached the
    API server pause it for `RELEASED_SANDBOX_PAUSE_SECONDS`, so a driver that has
    lost its connection does not sit at exit working through a queue of timeouts.
    """
    if not sandbox_name or not self._released_sandbox_deletes_enabled():
      return
    with self._released_sandbox_lock:
      if self._released_sandbox_pool is None:
        self._released_sandbox_pool = ThreadPoolExecutor(
            max_workers=RELEASED_SANDBOX_DELETE_WORKERS,
            thread_name_prefix=f"asrl-reclaim-{self.name}")
      pool = self._released_sandbox_pool
    try:
      pool.submit(self._delete_released_sandbox, sandbox_name, pod_name)
    except RuntimeError:  # interpreter shutting down; garbage collection takes over
      pass

  def _released_sandbox_deletes_enabled(self) -> bool:
    return (not self._released_sandbox_disabled
            and time.monotonic() >= self._released_sandbox_paused_until)

  def _delete_released_sandbox(self, sandbox_name: str, pod_name: str | None) -> None:
    if not self._released_sandbox_deletes_enabled():
      return
    try:
      self.custom_api.delete_namespaced_custom_object(
          group=constants.SANDBOX_GROUP, version=constants.SANDBOX_VERSION,
          namespace=self.namespace, plural=constants.SANDBOXES_PLURAL, name=sandbox_name,
          _request_timeout=RELEASED_SANDBOX_DELETE_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - best effort; garbage collection still runs
      if not self._released_sandbox_delete_failed("Sandbox", e):
        # The Sandbox may still exist, and its controller replaces a missing pod.
        return
    else:
      self._released_sandbox_reached_server()
    if not pod_name or self._released_sandbox_disabled:
      return
    try:
      # The pod is not parsed: at hundreds of releases a second, building the returned
      # V1Pod on these threads competes with the event loop for the interpreter lock.
      resp = self.core_api.delete_namespaced_pod(
          pod_name, self.namespace, grace_period_seconds=0,
          _preload_content=False, _request_timeout=RELEASED_SANDBOX_DELETE_TIMEOUT)
      # An unread body would make urllib3 discard the connection instead of reusing it.
      for finish in ("drain_conn", "release_conn"):
        fn = getattr(resp, finish, None)
        if callable(fn):
          fn()
    except Exception as e:  # noqa: BLE001 - best effort; garbage collection still runs
      self._released_sandbox_delete_failed("pod", e)
    else:
      self._released_sandbox_reached_server()

  def _released_sandbox_reached_server(self) -> None:
    with self._released_sandbox_lock:
      self._released_sandbox_errors = 0

  def _released_sandbox_delete_failed(self, kind: str, error: Exception) -> bool:
    """Record a failed delete; returns True when the object is already gone."""
    status = getattr(error, "status", None)
    reason = None
    with self._released_sandbox_lock:
      if status:
        self._released_sandbox_errors = 0
        if status == 403 and not self._released_sandbox_disabled:
          self._released_sandbox_disabled = True
          reason = f"no delete access to released {kind}s; leaving released sandboxes"
      else:
        self._released_sandbox_errors += 1
        if self._released_sandbox_errors >= RELEASED_SANDBOX_MAX_ERRORS:
          self._released_sandbox_errors = 0
          self._released_sandbox_paused_until = (
              time.monotonic() + RELEASED_SANDBOX_PAUSE_SECONDS)
          reason = (f"{RELEASED_SANDBOX_MAX_ERRORS} deletes in a row did not reach the "
                    f"API server; for {RELEASED_SANDBOX_PAUSE_SECONDS:.0f}s leaving "
                    "released sandboxes")
    if status == 404:
      return True
    if reason is not None:
      logger.info("cluster %s: %s to garbage collection", self.name, reason)
    else:
      logger.debug("cluster %s: deleting released %s failed: %s", self.name, kind, error)
    return False

  async def prewarm_async_clients(self, *, connections: int = 64) -> None:
    """Best-effort pre-initialization of ``WsApiClient`` and HTTP/1.1 TLS keep-alive sockets.

    Runs off the acquire hot path during ``prepare_clusters()`` when a cluster uses real
    ``kubernetes_asyncio`` clients (skipped for injected test doubles) so ``t=0`` bursts
    avoid cold CA/SSLContext loading and cold TCP+TLSv1.3 handshake storms.
    """
    if self._injected_sync_api_client or self._injected_async_api_client:
      return
    try:
      await self.get_async_sandbox_client()
      await self.async_exec_core_api()
    except Exception:  # noqa: BLE001
      return
    api = self._async_api_client
    if api is None or not type(api).__module__.startswith("kubernetes_asyncio"):
      return
    conn = getattr(
        getattr(getattr(api, "rest_client", None), "pool_manager", None),
        "connector",
        None,
    )
    conns_map = getattr(conn, "_conns", None)
    idle_count = sum(len(v) for v in conns_map.values()) if isinstance(conns_map, dict) else 0
    want = max(0, min(connections, 64) - idle_count)
    if want <= 0:
      return

    async def _ping() -> None:
      resp = await api.call_api(
          "/livez",
          "GET",
          auth_settings=["BearerToken"],
          _return_http_data_only=True,
          _preload_content=False,
          _request_timeout=2.0,
      )
      if hasattr(resp, "read") and hasattr(resp, "release"):
        try:
          await resp.read()
        except BaseException:
          if hasattr(resp, "close"):
            resp.close()
          raise
        finally:
          resp.release()

    await asyncio.gather(*(_ping() for _ in range(want)), return_exceptions=True)

  async def aclose(self) -> None:
    """Close any owned ``kubernetes_asyncio`` ``ApiClient`` and ``WsApiClient`` sessions."""
    cur_loop = asyncio.get_running_loop()
    with self._async_state_lock:
      target_loop = self._async_loop
      sb_client = self._async_sandbox_client
      api_client = None if self._injected_async_api_client else self._async_api_client
      ws_api = self._async_ws_api_client
      self._async_sandbox_client = None
      self._async_k8s_helper = None
      self._async_ws_api_client = None
      self._async_exec_core_api = None
      self._async_loop = None
      self._async_init_lock = None
      if not self._injected_async_api_client:
        self._async_api_client = None
    await self._close_async_bundle(
        cur_loop,
        target_loop if target_loop is not None else cur_loop,
        sb_client,
        api_client,
        ws_api,
        wait_cross_loop=True,
    )

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
