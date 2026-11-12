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

"""`AsyncSandboxFleet` — an awaitable, event-loop-native fleet.

Same surface as `SandboxFleet`, for RL frameworks (TorchRL/SkyRL/etc.) that own
an asyncio loop. It reuses the tested sync core, running each blocking k8s call on
a **dedicated thread pool sized to ``max_concurrent``** (see `_thread_pool` — *not*
``asyncio.to_thread``'s shared default pool, which is tied to the CPU count and
would starve/deadlock the `pipelined` strategy's overlapping warm/unwarm) and
driving real concurrency via ``asyncio.gather`` + a `Semaphore`. ``process_fn`` may
be sync or async.

(Design note: this is a thread-backed asyncio wrapper rather than a native
``kubernetes_asyncio`` rewrite — it delivers the same awaitable API + concurrency
with far less risk, reusing the cluster/resources/strategies logic verbatim. A
native async I/O backend can replace the internals later without changing this
API.)
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextlib
import functools
import inspect
import logging
import threading
import time
from typing import Awaitable
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from k8s_agent_sandbox import BatchError
from k8s_agent_sandbox.batch_utils import BATCH_REQUEST_TIMEOUT_SECONDS
from k8s_agent_sandbox.exceptions import SandboxNotReadyError
from k8s_agent_sandbox.utils import select_pod_ip

from . import constants
from .batch_claims import AsyncBatchClaim, AsyncBatchClaims, extract_batch_claim_name
from .cluster import Cluster
from .config import FleetConfig
from .exceptions import (
    FleetError,
    FleetOvercommitError,
    PoolNotFoundError,
    SandboxStartError,
)
from .fleet import SandboxFleet
from .handles import SandboxHandle
from .observability import repo_family
from .sources import Task

logger = logging.getLogger("agent_sandbox_rl.async_fleet")


class AsyncSandboxFleet:
  """Awaitable wrapper over `SandboxFleet`."""

  def __init__(self, config: FleetConfig | None = None, registry=None,
               *, sync_fleet: SandboxFleet | None = None):
    self._fleet = sync_fleet or SandboxFleet(config, registry)
    self._executor: ThreadPoolExecutor | None = None
    self._bg_tasks: set[asyncio.Task] = set()
    if self._fleet.config.claim_mode == "batch":
      self._async_batch_claims: AsyncBatchClaims | None = AsyncBatchClaims(
          self._fleet.config.batch_claims,
          ready_timeout=self._fleet.config.effective_claim_timeout(),
          labels=self._fleet.config.labels,
          fail_fast=self._fleet.config.fail_fast,
      )
      self._fleet._async_batch_claims = self._async_batch_claims
      if (
          "acquire" not in self._fleet.__dict__
          and type(self._fleet).acquire is SandboxFleet.acquire
      ):
        self._fleet._defer_sync_batch_prepare = True
    else:
      self._async_batch_claims = None

  def _sync_acquire_unpatched(self) -> bool:
    return (
        "acquire" not in self._fleet.__dict__
        and type(self._fleet).acquire is SandboxFleet.acquire
    )

  def _can_use_native_async_batch(self, cluster: object) -> bool:
    return (
        self._async_batch_claims is not None
        and self._sync_acquire_unpatched()
        and isinstance(cluster, Cluster)
        and cluster.supports_native_async_batch
    )

  async def _prepare_async_batch_clusters(
      self, entries, *, only_unopened: bool = False, just_warmed: bool = False
  ) -> None:
    if self._async_batch_claims is None or not entries or not self._sync_acquire_unpatched():
      return
    with self._fleet._lock:
      if self._fleet._torndown or self._fleet._async_tearing_down:
        return
      warmed_snapshot = (
          dict(self._fleet._warmed)
          if just_warmed and not self._fleet.config.adopt_existing
          else None
      )
    pools_by_cluster: dict[str, list[str]] = collections.defaultdict(list)
    verified_by_cluster: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for entry in entries:
      pools_by_cluster[entry.cluster].append(entry.pool)
      if warmed_snapshot is not None and entry.image in warmed_snapshot:
        verified_by_cluster[entry.cluster][entry.pool] = warmed_snapshot[entry.image]
    cluster_pools = []
    for cname, pools in pools_by_cluster.items():
      c = self._fleet.registry.get(cname)
      if self._can_use_native_async_batch(c):
        cluster_pools.append((c, pools))
    if not cluster_pools:
      return
    await self._async_batch_claims.prepare_clusters(
        cluster_pools,
        only_unopened=only_unopened,
        verified_ready=(dict(verified_by_cluster) if warmed_snapshot is not None else None),
    )

  async def _aclose_clusters(self) -> None:
    for c in self._fleet.registry:
      if isinstance(c, Cluster):
        await c.aclose()

  async def aclose(self) -> None:
    """Close owned async API sessions and shut down the dedicated thread pool."""
    try:
      await self._aclose_clusters()
    finally:
      self.close()

  # --- dedicated thread pool (NOT asyncio's default) --------------------- #
  def _thread_pool(self) -> ThreadPoolExecutor:
    """A thread pool sized to the fleet's concurrency, used for all blocking k8s
    calls — instead of ``asyncio.to_thread``'s shared default pool
    (``min(32, cpu+4)``). That default is tied to the driver's CPU count, not
    ``max_concurrent``: the ``pipelined`` strategy overlaps prefetch + process +
    unwarm, and ``wait_for_pool_ready`` holds its thread, so the default pool
    starves teardown and deadlocks. Size for ~process (max_concurrent) + two
    windows (prefetch + unwarm) + headroom."""
    if self._executor is None:
      mc = max(1, self._fleet.config.max_concurrent)
      win = self._fleet.config.window_size or mc
      workers = min(4096, max(64, mc + 64, mc + 2 * win + 16))
      self._executor = ThreadPoolExecutor(
          max_workers=workers, thread_name_prefix="asrl-async")
      logger.debug("async fleet thread pool: %d workers", workers)
    return self._executor

  async def _to_thread(self, fn, *args, **kwargs):
    """Run a blocking call on the dedicated pool (drop-in for asyncio.to_thread)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        self._thread_pool(), functools.partial(fn, *args, **kwargs))

  def close(self, *, wait: bool = True) -> None:
    """Shut down the dedicated thread pool (idempotent). Prefer ``async with`` /
    ``close()``; the fleet is reusable, so the pool persists across ``run()`` calls
    until then.

    ``wait=True`` (default, the explicit-close path) blocks until outstanding work
    finishes and cancels queued-but-unstarted work, so no non-daemon worker thread
    outlives the close. ``__del__`` passes ``wait=False`` to avoid hanging during
    GC/interpreter finalization.
    """
    if self._executor is not None:
      self._executor.shutdown(wait=wait, cancel_futures=True)
      self._executor = None

  def __del__(self):
    # Backstop for the no-context-manager path (`fleet.run()` then discard): don't
    # leak the pool's threads if close()/__aexit__ was never called. Non-blocking
    # so finalization can't hang on in-flight work.
    try:
      self.close(wait=False)
    except Exception:  # pragma: no cover - best-effort at finalization
      pass

  # --- sync passthroughs (cheap, no I/O) --------------------------------- #
  @property
  def config(self) -> FleetConfig:
    return self._fleet.config

  @property
  def registry(self):
    return self._fleet.registry

  @property
  def tasks(self) -> list[Task]:
    return self._fleet.tasks

  @property
  def plan_(self):
    return self._fleet.plan_

  @property
  def report(self):
    return self._fleet.report

  def load_tasks(self, source, *, image_rewrite=None, image_field=None,
                 id_field=None) -> list[Task]:
    return self._fleet.load_tasks(source, image_rewrite=image_rewrite,
                                  image_field=image_field, id_field=id_field)

  def add_tasks(self, source, *, image_rewrite=None) -> list[Task]:
    return self._fleet.add_tasks(source, image_rewrite=image_rewrite)

  async def wait_pools_ready(self, images, *, timeout: float | None = None,
                             replicas=None) -> dict[str, bool]:
    return await self._to_thread(self._fleet.wait_pools_ready, images,
                                 timeout=timeout, replicas=replicas)

  def image_counts(self):
    return self._fleet.image_counts()

  def handles(self) -> list[SandboxHandle]:
    return self._fleet.handles()

  def hostnames(self) -> list[str]:
    return self._fleet.hostnames()

  def endpoints(self, port: int = 8888) -> list[str]:
    return self._fleet.endpoints(port)

  # --- awaitable lifecycle (blocking calls offloaded to threads) --------- #
  async def preflight(self):
    return await self._to_thread(self._fleet.preflight)

  async def plan(self):
    return await self._to_thread(self._fleet.plan)

  async def ensure_templates(self):
    return await self._to_thread(self._fleet.ensure_templates)

  async def start_warmpools(self, wait: bool = True,
                            create_budget: int | None = None):
    res = await self._to_thread(self._fleet.start_warmpools, wait,
                                create_budget=create_budget)
    if wait and self._fleet.plan_ is not None:
      await self._prepare_async_batch_clusters(
          self._fleet.plan_.entries, only_unopened=False, just_warmed=True
      )
    return res

  async def prepull(self, wait: bool = True, **overrides):
    return await self._to_thread(self._fleet.prepull, wait, **overrides)

  async def prepull_delete(self):
    return await self._to_thread(self._fleet.prepull_delete)

  async def setup(self, prepull: bool = False) -> "AsyncSandboxFleet":
    self._fleet._defer_sync_batch_prepare = self._sync_acquire_unpatched()
    await self._to_thread(self._fleet.setup, prepull)
    if self._fleet.plan_ is not None:
      await self._prepare_async_batch_clusters(
          self._fleet.plan_.entries, just_warmed=True
      )
    return self

  def _reclaim_cancelled_future(
      self,
      pool: concurrent.futures.ThreadPoolExecutor,
      loop_thread: threading.Thread,
      done_fut: concurrent.futures.Future[SandboxHandle],
      *,
      caller: str,
  ) -> None:
    if done_fut.cancelled() or done_fut.exception() is not None:
      return
    handle = done_fut.result()

    def _safe_release() -> None:
      try:
        self._fleet.release(handle)
      except Exception:  # noqa: BLE001
        logger.warning("%s: failed to reclaim cancelled handle %s",
                       caller, handle.claim_name, exc_info=True)

    # When done_fut finishes on a ThreadPoolExecutor worker thread (including
    # while close(wait=True) is waiting in executor.shutdown(wait=True)),
    # release inline on that worker thread so close(wait=True) blocks until
    # the reclaim finishes without submitting to a shutting-down pool.
    if threading.current_thread() is not loop_thread:
      _safe_release()
      return
    try:
      rel_fut = pool.submit(_safe_release)
    except RuntimeError:
      threading.Thread(target=_safe_release, daemon=True).start()
    else:
      rel_fut.add_done_callback(
          lambda f: (
              threading.Thread(target=_safe_release, daemon=True).start()
              if f.cancelled() else None
          )
      )

  async def _acquire_native_batch(
      self,
      task: Task,
      cluster: Cluster,
      pool: str,
      entry,
      *,
      on_demand: bool = False,
      pod_annotations: dict | None = None,
  ) -> SandboxHandle:
    fl = self._fleet
    batch_claims = self._async_batch_claims
    assert batch_claims is not None
    with fl._lock:
      if fl._torndown or fl._async_tearing_down:
        raise FleetError("fleet was torn down; call setup() before acquiring again")
      hard = fl.config.max_live_sandboxes
      if hard and fl._claims_reserved >= hard:
        raise FleetOvercommitError(
            f"max_live_sandboxes={hard} reached: {fl._claims_reserved} claims "
            "live or in flight on this fleet. Release handles before acquiring "
            "more, or raise the limit.")
      fl._claims_reserved += 1

    annotations = dict(pod_annotations or {})
    fam = repo_family(task)
    claim: AsyncBatchClaim | None = None
    sandbox = None
    created_pool = False
    cluster_slot_reserved = False
    handle: SandboxHandle | None = None
    try:
      if on_demand:
        pool, created_pool = await self._to_thread(
            fl._ensure_ondemand_pool_for_acquire, cluster, task.image
        )
      with fl._obs.phase("claim", cluster=cluster.name, family=fam):
        claim = await batch_claims.acquire(
            cluster, pool, pod_annotations=annotations or None
        )
        v = batch_claims.pop_verdict(cluster.name, claim.member.claim_name)
        if v is not None:
          raise SandboxStartError(
              v.reason, v.message, claim_name=claim.member.claim_name,
              pod_name=v.pod_name, image=task.image)
        try:
          sandbox = await claim.batch.connect(claim.member)
        except SandboxNotReadyError as exc:
          raise BatchError(
              f"batch lost '{claim.member.claim_name}' before connect: {exc}"
          ) from exc
        pod = sandbox.sandbox_id
        pod_ip = select_pod_ip(claim.member.pod_ips)

      handle = SandboxHandle(
          task=task,
          cluster_name=cluster.name,
          claim_name=sandbox.claim_name or claim.member.claim_name,
          sandbox_id=sandbox.sandbox_id,
          pod_name=pod,
          hostname=sandbox.sandbox_id,
          pod_ip=pod_ip,
          sandbox=sandbox,
          _cluster=cluster,
          runtime_container=((entry.runtime_container if entry is not None else None)
                             or constants.RUNTIME_CONTAINER),
          _batch_claim=claim,
      )
      with fl._lock:
        if fl._torndown or fl._async_tearing_down:
          raise FleetError(
              f"fleet has torn down; cannot acquire on {cluster.name!r}")
        fl._handles[id(handle)] = handle
        cluster.reserve_claim()
        cluster_slot_reserved = True
    except BaseException as exc:  # noqa: BLE001
      with fl._lock:
        if cluster_slot_reserved:
          if handle is not None:
            fl._handles.pop(id(handle), None)
          cluster.release_claim()
        fl._claims_reserved = max(0, fl._claims_reserved - 1)
      if claim is not None or created_pool:
        async def _cleanup_failed_acquire() -> None:
          if claim is not None:
            try:
              await claim.arelease()
            except Exception:  # noqa: BLE001
              logger.warning(
                  "failed to release async batch member after acquire error",
                  exc_info=True)
            if sandbox is not None:
              close_conn = getattr(sandbox, "close_connection", None)
              if callable(close_conn):
                try:
                  res = close_conn()
                  if inspect.isawaitable(res):
                    await res
                except Exception:  # noqa: BLE001
                  logger.warning(
                      "failed to close async sandbox connection after acquire error",
                      exc_info=True)
          if created_pool:
            try:
              await self._to_thread(
                  fl._rollback_ondemand_pool, cluster, task.image, pool
              )
            except Exception:  # noqa: BLE001
              logger.warning(
                  "failed to roll back on-demand pool after async acquire error",
                  exc_info=True)

        cleanup_task = asyncio.create_task(_cleanup_failed_acquire())
        self._bg_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(self._bg_tasks.discard)
        with contextlib.suppress(asyncio.CancelledError):
          await asyncio.shield(cleanup_task)
      if isinstance(exc, Exception):
        if isinstance(exc, SandboxStartError):
          fl._obs.claim(cluster.name, "start_failed")
          raise
        failed_claim = extract_batch_claim_name(exc)
        b_verdict = (
            batch_claims.pop_verdict(cluster.name, failed_claim)
            if failed_claim is not None else None
        )
        if failed_claim is not None and b_verdict is not None:
          fl._obs.claim(cluster.name, "start_failed")
          raise SandboxStartError(
              b_verdict.reason, b_verdict.message, claim_name=failed_claim,
              pod_name=b_verdict.pod_name, image=task.image) from exc
      fl._obs.claim(cluster.name, "error")
      raise
    fl._obs.claim(cluster.name, "ok")
    return handle

  async def acquire(
      self,
      task: Task,
      *,
      snapshot_uid: str | None = None,
      pod_annotations: dict | None = None,
  ) -> SandboxHandle:
    fl = self._fleet
    if (
        self._async_batch_claims is not None
        and not snapshot_uid
        and self._sync_acquire_unpatched()
    ):
      entry = fl.plan_.for_image(task.image) if fl.plan_ else None
      if entry is not None:
        cluster = fl.registry.get(entry.cluster)
        if self._can_use_native_async_batch(cluster):
          return await self._acquire_native_batch(
              task, cluster, entry.pool, entry, pod_annotations=pod_annotations
          )
      elif fl.config.adopt_existing:
        with fl._lock:
          if fl._torndown or fl._async_tearing_down:
            raise FleetError(
                "fleet was torn down; call setup() before acquiring again"
            )
          hard = fl.config.max_live_sandboxes
          if hard and fl._claims_reserved >= hard:
            raise FleetOvercommitError(
                f"max_live_sandboxes={hard} reached: {fl._claims_reserved} claims "
                "live or in flight on this fleet. Release handles before acquiring "
                "more, or raise the limit."
            )
        raise PoolNotFoundError(
            f"adopt_existing: no adopted warm pool for image '{task.image}', and "
            f"adopt mode does not create one. The image is not in the current plan "
            f"— reload tasks and re-run plan(), or point the fleet at a namespace "
            f"that serves it.")
      else:
        with fl._lock:
          if fl._torndown or fl._async_tearing_down:
            raise FleetError(
                "fleet was torn down; call setup() before acquiring again"
            )
        candidate = fl.placement.select(task.image, fl.registry)
        if self._can_use_native_async_batch(candidate):
          return await self._acquire_native_batch(
              task,
              candidate,
              fl.config.pool_name(task.image),
              None,
              on_demand=True,
              pod_annotations=pod_annotations,
          )

    loop = asyncio.get_running_loop()
    loop_thread = threading.current_thread()
    pool = self._thread_pool()
    cf_fut = pool.submit(
        self._fleet.acquire,
        task,
        snapshot_uid=snapshot_uid,
        pod_annotations=pod_annotations,
    )
    try:
      return await asyncio.wrap_future(cf_fut, loop=loop)
    except BaseException:
      cf_fut.cancel()
      cf_fut.add_done_callback(
          functools.partial(
              self._reclaim_cancelled_future,
              pool,
              loop_thread,
              caller="acquire",
          )
      )
      raise

  async def acquire_batch(self, tasks: list[Task]) -> list[SandboxHandle]:
    task_list = list(tasks)
    if not task_list:
      return []
    sem = asyncio.Semaphore(max(1, self._fleet.config.max_concurrent))
    abort = False
    first_err: BaseException | None = None
    atasks: list[asyncio.Task[SandboxHandle]] = []
    started: set[asyncio.Task[SandboxHandle]] = set()

    async def _one(t: Task) -> SandboxHandle:
      nonlocal abort, first_err
      async with sem:
        if abort:
          raise asyncio.CancelledError()
        me = asyncio.current_task()
        if me is not None:
          started.add(me)
        try:
          return await self.acquire(t)
        except Exception as exc:
          if first_err is None:
            first_err = exc
            abort = True
            for sibling in atasks:
              if (
                  sibling is not me
                  and sibling not in started
                  and not sibling.done()
              ):
                sibling.cancel()
          raise

    atasks = [asyncio.create_task(_one(t)) for t in task_list]
    try:
      outcomes = await asyncio.gather(*atasks, return_exceptions=True)
    except BaseException as exc:
      abort = True
      if first_err is None:
        first_err = exc
      for sibling in atasks:
        if not sibling.done():
          sibling.cancel()
      try:
        outcomes = await asyncio.gather(*atasks, return_exceptions=True)
      except BaseException:
        pool = self._thread_pool()
        loop_thread = threading.current_thread()
        for sibling in atasks:
          if (
              sibling.done()
              and not sibling.cancelled()
              and sibling.exception() is None
          ):
            h = sibling.result()
            if isinstance(h, SandboxHandle):
              drain_h: concurrent.futures.Future[SandboxHandle] = (
                  concurrent.futures.Future()
              )
              drain_h.set_result(h)
              self._reclaim_cancelled_future(
                  pool, loop_thread, drain_h, caller="acquire_batch")
        raise

    if first_err is None:
      first_err = next((o for o in outcomes if isinstance(o, BaseException)), None)
    if first_err is not None:
      acquired = [o for o in outcomes if isinstance(o, SandboxHandle)]
      if acquired:
        released_all = False
        try:
          rb = await asyncio.gather(
              *(self.release(h) for h in acquired), return_exceptions=True)
          released_all = True
          for h, res in zip(acquired, rb):
            if isinstance(res, BaseException):
              logger.warning("acquire_batch: failed to roll back handle %s: %s",
                             h.claim_name, res)
        except BaseException:
          if not released_all:
            pool = self._thread_pool()
            loop_thread = threading.current_thread()
            for h in acquired:
              done_h: concurrent.futures.Future[SandboxHandle] = (
                  concurrent.futures.Future()
              )
              done_h.set_result(h)
              self._reclaim_cancelled_future(
                  pool, loop_thread, done_h, caller="acquire_batch")
          raise
      raise first_err
    return [o for o in outcomes if isinstance(o, SandboxHandle)]

  async def release(self, handle: SandboxHandle, *, delete_snapshots: bool = False):
    fl = self._fleet
    if (
        not delete_snapshots
        and "release" not in fl.__dict__
        and type(fl).release is SandboxFleet.release
        and isinstance(handle._batch_claim, AsyncBatchClaim)
        and "release" not in handle.__dict__
        and type(handle).release is SandboxHandle.release
    ):
      with fl._lock:
        if fl._handles.pop(id(handle), None) is None:
          return
        c = fl.registry.get(handle.cluster_name)
      try:
        with fl._obs.phase("release", cluster=handle.cluster_name):
          await handle.arelease()
      except BaseException:
        with fl._lock:
          fl._handles[id(handle)] = handle
        raise
      if not handle._slot_released:
        c.release_claim()
        with fl._lock:
          fl._claims_reserved = max(0, fl._claims_reserved - 1)
      return
    return await self._to_thread(self._fleet.release, handle,
                                 delete_snapshots=delete_snapshots)

  async def _afinalize_claimed_batch_handles(self, claimed: list[SandboxHandle]) -> None:
    fl = self._fleet
    released_slots = 0
    for h in claimed:
      await h.afinalize_bulk_batch_release()
      if not h._slot_released:
        fl.registry.get(h.cluster_name).release_claim()
        released_slots += 1
    if released_slots:
      with fl._lock:
        fl._claims_reserved = max(0, fl._claims_reserved - released_slots)

  async def release_all(self):
    fl = self._fleet
    handles = self.handles()
    if not handles:
      return
    unpatched = (
        "release" not in self.__dict__
        and type(self).release is AsyncSandboxFleet.release
        and "release" not in fl.__dict__
        and type(fl).release is SandboxFleet.release
        and "release_all" not in fl.__dict__
        and type(fl).release_all is SandboxFleet.release_all
    )
    if unpatched and any(isinstance(h._batch_claim, AsyncBatchClaim) for h in handles):
      by_batch: dict[int, tuple[object, str, list[SandboxHandle]]] = {}
      remaining: list[SandboxHandle] = []
      excluded_batches: set[int] = set()
      for h in handles:
        bc = h._batch_claim
        if (
            isinstance(bc, AsyncBatchClaim)
            and "release" not in h.__dict__
            and type(h).release is SandboxHandle.release
            and "release_member" not in getattr(bc.batch, "__dict__", {})
            and bc.batch.err() is None
            and callable(getattr(bc.batch, "release_members", None))
        ):
          key = id(bc.batch)
          if key not in by_batch:
            by_batch[key] = (bc.batch, h.cluster_name, [])
          by_batch[key][2].append(h)
        else:
          if isinstance(bc, AsyncBatchClaim):
            excluded_batches.add(id(bc.batch))
          remaining.append(h)

      bulk_groups: list[tuple[object, str, list[SandboxHandle]]] = []
      for key, (b, cname, gh) in by_batch.items():
        if key not in excluded_batches and len(gh) >= 2:
          bulk_groups.append((b, cname, gh))
        else:
          remaining.extend(gh)

      async def _release_async_group(batch_obj, cname: str, gh: list[SandboxHandle]) -> None:
        with fl._lock:
          claimed = [h for h in gh if fl._handles.pop(id(h), None) is not None]
        if not claimed:
          return
        try:
          with fl._obs.phase("release", cluster=cname):
            await getattr(batch_obj, "release_members")()
        except BaseException:
          with fl._lock:
            for h in claimed:
              fl._handles[id(h)] = h
          raise
        await self._afinalize_claimed_batch_handles(claimed)

      coros = [_release_async_group(b, cname, gh) for b, cname, gh in bulk_groups]
      coros.extend(self.release(h) for h in remaining)
      outcomes = await asyncio.gather(*coros, return_exceptions=True)
      first_err = next((o for o in outcomes if isinstance(o, BaseException)), None)
      if first_err is not None:
        raise first_err
      return

    if fl._batch_claims is not None and unpatched:
      return await self._to_thread(self._fleet.release_all)
    outcomes = await asyncio.gather(
        *(self.release(h) for h in handles), return_exceptions=True)
    first_err = next((o for o in outcomes if isinstance(o, BaseException)), None)
    if first_err is not None:
      raise first_err

  # --- GKE Pod Snapshots (blocking SDK calls, run on the pool) ------------- #
  # The handle's snapshot methods block (the SDK extension is synchronous); use
  # these from an event loop instead of calling the handle directly.
  async def snapshot(self, handle: SandboxHandle, name: str | None = None, *,
                     timeout: int = 180) -> str:
    return await self._to_thread(self._fleet.snapshot, handle, name, timeout=timeout)

  async def suspend(self, handle: SandboxHandle, *, snapshot: bool = True,
                    timeout: int = 180):
    return await self._to_thread(self._fleet.suspend, handle, snapshot=snapshot,
                                 timeout=timeout)

  async def resume(self, handle: SandboxHandle, *, timeout: int = 180) -> bool:
    return await self._to_thread(self._fleet.resume, handle, timeout=timeout)

  async def restore(self, handle: SandboxHandle, snapshot_uid: str, *,
                    timeout: int = 180) -> None:
    return await self._to_thread(self._fleet.restore, handle, snapshot_uid,
                                 timeout=timeout)

  async def teardown(self, delete_namespace: bool = False, *, verify: bool = False):
    fl = self._fleet
    async_release_err: Exception | None = None
    do_async_drain = False
    if self._async_batch_claims is not None:
      with fl._lock:
        if not fl._torndown and not fl._async_tearing_down:
          fl._async_tearing_down = True
          do_async_drain = True
      if do_async_drain:
        self._async_batch_claims.mark_closing()
    try:
      if do_async_drain and self._async_batch_claims is not None:
        deferred_async: dict[int, list[SandboxHandle]] = {}
        if (
            "release_all" not in self.__dict__
            and type(self).release_all is AsyncSandboxFleet.release_all
            and "release" not in self.__dict__
            and type(self).release is AsyncSandboxFleet.release
            and "release_all" not in fl.__dict__
            and type(fl).release_all is SandboxFleet.release_all
            and "release" not in fl.__dict__
            and type(fl).release is SandboxFleet.release
        ):
          by_batch: dict[int, list[SandboxHandle]] = {}
          excluded_batches: set[int] = set()
          for h in self.handles():
            bc = h._batch_claim
            if not isinstance(bc, AsyncBatchClaim):
              continue
            if (
                "release" not in h.__dict__
                and type(h).release is SandboxHandle.release
                and "release_member" not in getattr(bc.batch, "__dict__", {})
                and bc.batch.err() is None
            ):
              by_batch.setdefault(id(bc.batch), []).append(h)
            else:
              excluded_batches.add(id(bc.batch))
          bulk_candidates = [
              h
              for bid, gh in by_batch.items()
              if bid not in excluded_batches
              for h in gh
          ]
          if bulk_candidates:
            with fl._lock:
              claimed = [
                  h for h in bulk_candidates
                  if fl._handles.pop(id(h), None) is not None
              ]
            if claimed:
              for h in claimed:
                assert h._batch_claim is not None
                deferred_async.setdefault(id(h._batch_claim.batch), []).append(h)
              await self._afinalize_claimed_batch_handles(claimed)
        failed_async = await self._async_batch_claims.release_all()
        for bid, exc in failed_async.items():
          failed_handles = deferred_async.get(bid)
          if not failed_handles:
            continue
          if async_release_err is None:
            async_release_err = exc
          if fl._shared_run_id:
            for h in failed_handles:
              cluster_obj = fl.registry.get(h.cluster_name)
              helper = getattr(cluster_obj, "_async_k8s_helper", None)
              try:
                if helper is not None:
                  await helper.delete_sandbox_claim(
                      h.claim_name,
                      cluster_obj.namespace,
                      _request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS,
                      log_level=logging.DEBUG,
                      batch=True,
                  )
                else:
                  await self._to_thread(
                      cluster_obj.resources.delete_claim,
                      h.claim_name,
                      request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS,
                  )
              except Exception:  # noqa: BLE001
                logger.warning(
                    "teardown: fallback claim delete failed for %s on %s",
                    h.claim_name, h.cluster_name, exc_info=True,
                )
      res = await self._to_thread(
          self._fleet.teardown,
          delete_namespace,
          verify=(verify and async_release_err is None),
      )
    finally:
      if do_async_drain:
        with fl._lock:
          fl._async_tearing_down = False
      await self._aclose_clusters()
    if async_release_err is not None:
      raise async_release_err
    return res

  async def verify_teardown(self):
    return await self._to_thread(self._fleet.verify_teardown)

  async def __aenter__(self) -> "AsyncSandboxFleet":
    return await self.setup()

  async def __aexit__(self, *exc):
    try:
      await self.teardown()
    finally:
      await self.aclose()

  # --- parallel processing + strategies ---------------------------------- #
  async def _call(self, process_fn, task, handle):
    # Fast path: plain coroutine functions and callable objects with `async
    # __call__` are awaited directly on the loop.
    if inspect.iscoroutinefunction(process_fn) or \
        inspect.iscoroutinefunction(getattr(process_fn, "__call__", None)):
      return await process_fn(task, handle)
    # Otherwise run in a worker thread — but some callables that aren't
    # coroutine-functions still RETURN an awaitable (e.g. functools.partial of an
    # async fn, or a sync fn returning a coroutine). Await the result if so,
    # rather than handing back an un-awaited coroutine.
    result = await self._to_thread(process_fn, task, handle)
    if inspect.isawaitable(result):
      return await result
    return result

  async def _process_parallel(self, tasks, process_fn, concurrency, *, _defer_tail: bool = True):
    total = len(tasks)
    sem = asyncio.Semaphore(max(1, concurrency))
    results = [None] * total
    obs = self._fleet._obs
    fl = self._fleet
    can_defer_tail = (
        _defer_tail
        and (self._async_batch_claims is not None or fl._batch_claims is not None)
        and total >= 2
        and not self.handles()
        and "release" not in self.__dict__
        and type(self).release is AsyncSandboxFleet.release
        and "release_all" not in self.__dict__
        and type(self).release_all is AsyncSandboxFleet.release_all
        and "release" not in fl.__dict__
        and type(fl).release is SandboxFleet.release
        and "release_all" not in fl.__dict__
        and type(fl).release_all is SandboxFleet.release_all
    )
    entered = 0
    deferred_tail: list[tuple[int, str, str, float, SandboxHandle]] = []

    async def _release_deferred_tail(*, capture_errors: bool) -> None:
      if not deferred_tail:
        return
      items = list(deferred_tail)
      deferred_tail.clear()
      item_by_hid = {id(h): (idx, cl, fm, start_t, h) for idx, cl, fm, start_t, h in items}
      with fl._lock:
        active_by_batch: dict[int, set[int]] = {}
        for live_h in fl._handles.values():
          if live_h._batch_claim is not None:
            active_by_batch.setdefault(id(live_h._batch_claim.batch), set()).add(id(live_h))

      by_batch: dict[int, tuple[object, str, list[SandboxHandle]]] = {}
      remaining: list[SandboxHandle] = []
      for _idx, cl, _fm, _st, h in items:
        bc = h._batch_claim
        if (
            isinstance(bc, AsyncBatchClaim)
            and "release" not in h.__dict__
            and type(h).release is SandboxHandle.release
            and "release_member" not in getattr(bc.batch, "__dict__", {})
            and bc.batch.err() is None
            and callable(getattr(bc.batch, "release_members", None))
            and active_by_batch.get(id(bc.batch), set()).issubset(item_by_hid.keys())
        ):
          key = id(bc.batch)
          if key not in by_batch:
            by_batch[key] = (bc.batch, cl, [])
          by_batch[key][2].append(h)
        else:
          remaining.append(h)

      bulk_groups: list[tuple[object, str, list[SandboxHandle]]] = []
      for _key, (b_obj, cname, gh) in by_batch.items():
        if len(gh) >= 2:
          bulk_groups.append((b_obj, cname, gh))
        else:
          remaining.extend(gh)

      rel_errors: dict[int, Exception] = {}

      async def _rel_bulk(b_obj, cname: str, gh: list[SandboxHandle]) -> None:
        with fl._lock:
          claimed = [h for h in gh if fl._handles.pop(id(h), None) is not None]
        if not claimed:
          return
        try:
          with obs.phase("release", cluster=cname):
            await getattr(b_obj, "release_members")()
        except Exception as e:
          with fl._lock:
            for h in claimed:
              fl._handles[id(h)] = h
          if not capture_errors:
            raise
          for h in claimed:
            rel_errors[id(h)] = e
          return
        except BaseException:
          with fl._lock:
            for h in claimed:
              fl._handles[id(h)] = h
          raise
        await self._afinalize_claimed_batch_handles(claimed)

      async def _rel_one(h: SandboxHandle) -> None:
        try:
          await self.release(h)
        except Exception as e:
          if not capture_errors:
            raise
          rel_errors[id(h)] = e

      coros = [_rel_bulk(b_obj, cname, gh) for b_obj, cname, gh in bulk_groups]
      coros.extend(_rel_one(h) for h in remaining)
      await asyncio.gather(*coros)

      if capture_errors:
        now = time.monotonic()
        for idx, cl, fm, start_t, h in items:
          err = rel_errors.get(id(h))
          if err is not None:
            logger.error("task %s release failed: %s", tasks[idx].id, err)
            results[idx] = err
            obs.task_done(cl, fm, "error", now - start_t)
          else:
            obs.task_done(cl, fm, "ok", now - start_t)

    async def _one(i, task):
      nonlocal entered
      fam = repo_family(task)
      t0 = time.monotonic()
      status = "ok"
      cluster = "-"
      deferred = False
      async with sem:
        entered += 1
        try:
          handle = await self.acquire(task)
          cluster = handle.cluster_name
          task_failed = False
          try:
            with obs.phase("process", cluster=cluster, family=fam):
              results[i] = await self._call(process_fn, task, handle)
          except BaseException:
            task_failed = True
            raise
          finally:
            if (
                not task_failed
                and can_defer_tail
                and entered >= total
                and handle._batch_claim is not None
            ):
              deferred = True
              deferred_tail.append((i, cluster, fam, t0, handle))
            else:
              await self.release(handle)
        except Exception as e:  # noqa: BLE001
          status = "error"
          logger.error("task %s failed: %s", task.id, e)
          results[i] = e
        finally:
          if not deferred:
            obs.task_done(cluster, fam, status, time.monotonic() - t0)

    try:
      await asyncio.gather(*(_one(i, t) for i, t in enumerate(tasks)))
      if deferred_tail:
        await _release_deferred_tail(capture_errors=True)
    finally:
      if deferred_tail:
        await asyncio.gather(_release_deferred_tail(capture_errors=False), return_exceptions=True)
    return results

  async def _run_windowed(self, process_fn, concurrency, window,
                          replicas_override=None, *, teardown=True, executor=None):
    executor = executor or self._process_parallel
    self._fleet._defer_sync_batch_prepare = self._sync_acquire_unpatched()
    await self.preflight()
    await self.plan()
    images = list(self.image_counts().keys())
    by_image: dict[str, list] = {}
    for i, t in enumerate(self.tasks):
      by_image.setdefault(t.image, []).append((i, t))   # keep original index
    # Write results back at each task's original index so the returned list
    # matches self.tasks order (the "one result per task" contract), matching
    # the sync sliding implementation.
    results = [None] * len(self.tasks)
    try:
      for start in range(0, len(images), window):
        batch = images[start:start + window]
        # Route through the bounded batch warmer (capped by max_concurrent) rather
        # than one _to_thread per image, so a large window can't fan out hundreds
        # of concurrent API watches at once.
        await self._to_thread(self._fleet.warm_images, batch,
                              replicas_override=replicas_override, wait=True)
        if self._fleet.plan_ is not None:
          win_entries = [
              e for img in batch
              if (e := self._fleet.plan_.for_image(img)) is not None
          ]
          await self._prepare_async_batch_clusters(
              win_entries, only_unopened=False, just_warmed=True
          )
        batch_pairs = [(i, t) for img in batch for (i, t) in by_image[img]]
        batch_tasks = [t for _i, t in batch_pairs]
        if executor is self._process_parallel and len(batch_tasks) <= 128:
          batch_results = await self._process_parallel(
              batch_tasks, process_fn, concurrency, _defer_tail=False
          )
        else:
          batch_results = await executor(batch_tasks, process_fn, concurrency)
        for (i, _t), r in zip(batch_pairs, batch_results, strict=True):
          results[i] = r
        await self._to_thread(self._fleet.unwarm_images, batch)
    finally:
      if teardown:
        await self.teardown()
    return results

  async def _run_pipelined(self, process_fn, concurrency, window,
                           replicas_override=None, *, teardown=True, executor=None):
    """Async double-buffered sliding window: prefetch window N+1's pools (an
    ``asyncio.Task``) while window N's tasks run. Footprint ≤ 2 windows — the
    current window is unwarmed before awaiting the next prefetch, and only one
    prefetch task is ever in flight."""
    executor = executor or self._process_parallel
    self._fleet._defer_sync_batch_prepare = self._sync_acquire_unpatched()
    await self.preflight()
    await self.plan()
    images = list(self.image_counts().keys())
    by_image: dict[str, list] = {}
    for i, t in enumerate(self.tasks):
      by_image.setdefault(t.image, []).append((i, t))   # keep original index
    results = [None] * len(self.tasks)
    batches = [images[s:s + window] for s in range(0, len(images), window)]

    async def _warm(batch):
      # Bounded batch warm (capped by max_concurrent), not one task per image.
      await self._to_thread(self._fleet.warm_images, batch,
                            replicas_override=replicas_override, wait=True)
      if self._fleet.plan_ is not None:
        win_entries = [
            e for img in batch
            if (e := self._fleet.plan_.for_image(img)) is not None
        ]
        await self._prepare_async_batch_clusters(
            win_entries, only_unopened=False, just_warmed=True
        )

    async def _prefetch(batch):              # background warm, timed as "prefetch"
      with self._fleet._obs.phase("prefetch"):
        await _warm(batch)

    pending = None
    try:
      if batches:
        await _warm(batches[0])              # prime window 0 in the foreground
      for n, batch in enumerate(batches):
        pending = (asyncio.create_task(_prefetch(batches[n + 1]))
                   if n + 1 < len(batches) else None)
        batch_pairs = [(i, t) for img in batch for (i, t) in by_image[img]]
        batch_tasks = [t for _i, t in batch_pairs]
        if executor is self._process_parallel and len(batch_tasks) <= 128:
          batch_results = await self._process_parallel(
              batch_tasks, process_fn, concurrency, _defer_tail=False
          )
        else:
          batch_results = await executor(batch_tasks, process_fn, concurrency)
        for (i, _t), r in zip(batch_pairs, batch_results, strict=True):
          results[i] = r
        await self._to_thread(self._fleet.unwarm_images, batch)
        if pending is not None:
          await pending                      # surface prefetch errors inside try
          pending = None
    finally:
      if pending is not None:
        # Let the in-flight prefetch finish before teardown: cancelling the task
        # would not stop its warm_image already running on the thread pool, which
        # could create a pool *after* teardown's sweep and leak it. wait_pool_ready
        # is bounded by ready_timeout, so this can't hang indefinitely.
        await asyncio.gather(pending, return_exceptions=True)
      if teardown:
        await self.teardown()
    return results

  async def _run_once(self, strategy, process_fn, conc, *, teardown, executor=None):
    executor = executor or self._process_parallel
    if strategy == "naive":
      try:
        await self.setup()
        return await executor(self.tasks, process_fn, conc)
      finally:
        if teardown:
          await self.teardown()
    if strategy == "sliding":
      return await self._run_windowed(
          process_fn, conc, self._fleet.recommended_window(), teardown=teardown,
          executor=executor)
    if strategy == "pipelined":
      return await self._run_pipelined(
          process_fn, conc, self._fleet.recommended_window(pipelined=True),
          teardown=teardown, executor=executor)
    if strategy == "none":
      return await self._run_windowed(
          process_fn, conc, 1, replicas_override=1, teardown=teardown,
          executor=executor)
    raise ValueError(f"unknown strategy '{strategy}'")

  async def run(self, process_fn: Callable[[Task, SandboxHandle], object | Awaitable],
                strategy: str = "naive", concurrency: int | None = None,
                *, epochs: int = 1, keep_warm: bool = False,
                recycle: bool = False, reset=None, max_reuses: int = 32,
                reset_timeout: float = 5.0, use_session: bool = True,
                scale_on_hold: bool = True, shards_per_image: int = 1,
                claim_concurrency: int = 0) -> list:
    """Run all loaded tasks under ``strategy`` with up to ``concurrency``
    concurrent claim+exec. ``process_fn`` may be sync or a coroutine function.

    ``epochs``/``keep_warm`` mirror `SandboxFleet.run`: ``epochs>1`` runs N passes
    keeping pools resident between them (returns ``list[list]``); ``keep_warm=True``
    skips the final teardown for caller-driven reuse.

    ``recycle`` is an **orthogonal** modifier (not a strategy): the chosen
    ``strategy`` still governs warm-pool sizing/timing, but same-image tasks reuse
    one claimed sandbox (git-restore reset between them) via
    `recycle.reuse_git_restore_sandbox_async` instead of a fresh claim per task. It
    applies to multi-task-per-image workloads (RL rollouts) and is a no-op for 1:1
    eval, so it is off by default. ``reset``/``max_reuses``/``reset_timeout``/
    ``use_session``/``scale_on_hold``/``shards_per_image``/``claim_concurrency`` are
    forwarded to the async recycle executor and ignored when ``recycle=False``.
    """
    if epochs < 1:
      raise ValueError("epochs must be >= 1")
    conc = concurrency or self.config.max_concurrent
    obs = self._fleet._obs
    executor = None
    if recycle:                                # swap task→sandbox binding, keep warming
      from .recycle import GitRestoreReset, reuse_git_restore_sandbox_async
      _reset = reset or GitRestoreReset()

      async def executor(tasks, process_fn, concurrency):  # noqa: E306
        return await reuse_git_restore_sandbox_async(
            self, tasks, process_fn, concurrency, reset=_reset,
            max_reuses=max_reuses, reset_timeout=reset_timeout,
            use_session=use_session, scale_on_hold=scale_on_hold,
            shards_per_image=shards_per_image, claim_concurrency=claim_concurrency)
    if self._fleet.plan_ is None:              # give the circuit breaker a footprint
      await self.plan()
    expected = self._fleet.plan_.total_replicas if self._fleet.plan_ else None
    with self._fleet.overcommit_guard(expected), obs.run(strategy) as report:
      self._fleet.report = report
      try:
        report.environment = await self._to_thread(self._fleet.describe_environment)
      except Exception:  # noqa: BLE001 — environment is best-effort
        logger.debug("could not collect environment", exc_info=True)
      if epochs == 1:
        results = await self._run_once(strategy, process_fn, conc,
                                       teardown=not keep_warm, executor=executor)
      else:
        results = []
        for e in range(epochs):
          last = e == epochs - 1
          logger.info("epoch %d/%d", e + 1, epochs)
          try:
            results.append(await self._run_once(
                strategy, process_fn, conc, teardown=last and not keep_warm,
                executor=executor))
          except BaseException:               # a mid-run epoch never tore down
            if not keep_warm and not last:
              await self.teardown()
            raise
    logger.info("\n%s", report.summary())
    return results
