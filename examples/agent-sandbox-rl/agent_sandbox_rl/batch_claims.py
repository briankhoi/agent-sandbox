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

"""Batch claiming behind `SandboxFleet.acquire` (``FleetConfig(claim_mode="batch")``).

Each fleet holds one SDK batch per cluster. An acquire adds one member to it with
`SandboxBatch.acquire`, so every claim's readiness comes from the batch's one watch
instead of a watch per claim, and the claims are owned by the batch Lease, so the
batch reaper cleans up after a driver that dies without tearing down.

When warm pools are started via `SandboxFleet.setup` / `start_warmpools(wait=True)`
or the first window of `warm_images(wait=True)`, each cluster's batch is pre-opened
with those pools as size-0 groups (`claim_batch` prechecks them concurrently up to
`max_in_flight` and seeds `WarmCredit` + `_checked_pools` off the acquire hot path).
Any unplanned pool acquired later still opens or checks lazily on its first acquire.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
import inspect
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from k8s_agent_sandbox import BatchError, BatchGroup, Member, SandboxBatch
from k8s_agent_sandbox.batch_state import still_reaches
from k8s_agent_sandbox.batch_utils import (
    BATCH_POOL_HEADROOM,
    BATCH_REQUEST_TIMEOUT_SECONDS,
    BATCH_SHUTDOWN_MARGIN_SECONDS,
)
from k8s_agent_sandbox.sandbox_batch import _delete_batch_objects as _delete_batch_objects_sync
from k8s_agent_sandbox.utils import select_pod_ip

from .exceptions import FleetError
from .failfast import BatchWatchdog, StartVerdict

if TYPE_CHECKING:
  from .cluster import Cluster
  from .config import BatchClaimConfig, FailFastPolicy

logger = logging.getLogger("agent_sandbox_rl.batch_claims")

_RELEASED_MEMBER_RE = re.compile(
    r"(?:acquired member|while acquiring|for claim|batch lost) '([^']+)'"
)


def extract_batch_claim_name(exc: BaseException) -> str | None:
  """Extract the claim name from an SDK ``BatchError`` / ``TerminalMemberError``."""
  member = getattr(exc, "member", None)
  claim_name = getattr(member, "claim_name", None) if member is not None else None
  if isinstance(claim_name, str) and claim_name:
    return claim_name
  if isinstance(exc, BatchError):
    match = _RELEASED_MEMBER_RE.search(str(exc))
    if match is not None:
      return match.group(1)
  return None


@dataclass
class BatchClaim:
  """One acquired batch member and the batch that owns it."""

  batch: SandboxBatch
  member: Member
  # time.monotonic() by which the controller deletes the claim (its shutdownTime),
  # measured from before the create with a 1 s margin for sub-second truncation.
  expires_at: float
  cluster: "Cluster" = field(repr=False)
  rebaselined: bool = False

  def still_reaches(self) -> bool:
    """Whether a healthy batch's watch still maps this claim to its acquired pod."""
    # After refresh() the pod IP can differ from member.pod_ips, and a stopped
    # batch's watch no longer updates, so is_alive() checks the pod instead.
    if self.rebaselined or self.batch.err() is not None:
      return True
    current = self.batch.get_member(self.member.claim_name)
    return still_reaches(
        current, self.member.sandbox_name, select_pod_ip(self.member.pod_ips))

  def release(self) -> None:
    try:
      self.batch.release_member(self.member)
    except BatchError:
      # Once a batch stops (for example on BatchLeaseExpiredError) or teardown
      # releases it, release_member raises BatchError so a detached handle cannot
      # race a get_batch() takeover. BatchClaims generates random batch IDs and
      # never detaches or re-attaches, so deleting the claim directly cannot hit
      # another holder's recreated claim and tolerates 404 when already deleted.
      self.cluster.resources.delete_claim(
          self.member.claim_name,
          request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS)


class BatchClaims:
  """The batches one fleet claims through, one live batch per cluster."""

  def __init__(self, settings: "BatchClaimConfig", *, ready_timeout: int,
               labels: dict[str, str],
               fail_fast: "FailFastPolicy | None" = None) -> None:
    self._settings = settings
    self._ready_timeout = ready_timeout
    self._labels = labels
    self._fail_fast = fail_fast
    self._lock = threading.Lock()
    self._cluster_locks: dict[str, threading.Lock] = {}
    self._live: dict[str, SandboxBatch] = {}
    self._watchdogs: dict[str, BatchWatchdog] = {}
    self._verdicts: dict[tuple[str, str], StartVerdict] = {}
    # Every batch opened, including ones replaced after an error, so teardown
    # releases their remaining claims and Leases.
    self._opened: list[SandboxBatch] = []
    # Set by release_all, so an acquire racing teardown can't open a batch that
    # no later teardown would release and whose Lease would keep renewing.
    self._closed = False
    self._epoch: int = 0

  def reopen(self) -> None:
    """Lets acquires open batches again after `release_all`, for a new setup()."""
    with self._lock:
      self._closed = False

  def _record_verdict(
      self, cluster_name: str, claim_name: str, verdict: StartVerdict
  ) -> None:
    with self._lock:
      if (
          (cluster_name, claim_name) not in self._verdicts
          and len(self._verdicts) >= 4096
      ):
        self._verdicts.pop(next(iter(self._verdicts)), None)
      self._verdicts[(cluster_name, claim_name)] = verdict

  def pop_verdict(self, cluster_name: str, claim_name: str) -> StartVerdict | None:
    """Pop any fail-fast `StartVerdict` recorded for ``(cluster_name, claim_name)``."""
    with self._lock:
      return self._verdicts.pop((cluster_name, claim_name), None)

  def _cluster_lock(self, name: str) -> threading.Lock:
    with self._lock:
      return self._cluster_locks.setdefault(name, threading.Lock())

  def has_live_batch(self, cluster_name: str) -> bool:
    """Whether ``cluster_name`` currently has a healthy open batch."""
    with self._lock:
      existing = self._live.get(cluster_name)
    return existing is not None and existing.err() is None

  def prepare_clusters(
      self,
      cluster_pools: Sequence[tuple["Cluster", Sequence[str]]],
      *,
      only_unopened: bool = False,
      verified_ready: dict[str, dict[str, int]] | None = None,
  ) -> None:
    """Pre-open each cluster's batch with its warmed ``pools`` as size-0 groups."""
    pending: list[tuple["Cluster", list[str], dict[str, int] | None]] = []
    for cluster, pools in cluster_pools:
      deduped = list(dict.fromkeys(p for p in pools if p))
      if not deduped:
        continue
      if only_unopened and self.has_live_batch(cluster.name):
        continue
      vmap = verified_ready.get(cluster.name) if verified_ready else None
      pending.append((cluster, deduped, vmap))
    if not pending:
      return
    if len(pending) == 1:
      cluster, deduped, vmap = pending[0]
      self._batch_for(cluster, deduped, verified_ready=vmap)
      return
    first_err: BaseException | None = None
    with ThreadPoolExecutor(max_workers=len(pending)) as ex:
      futs = [
          ex.submit(self._batch_for, c, p, verified_ready=vm)
          for c, p, vm in pending
      ]
      for fut in futs:
        try:
          fut.result()
        except BaseException as exc:  # noqa: BLE001 (drain remaining clusters first)
          if first_err is None:
            first_err = exc
    if first_err is not None:
      raise first_err

  def _batch_for(self, cluster: "Cluster", pool: str | Sequence[str], *,
                 verified_ready: dict[str, int] | None = None) -> SandboxBatch:
    pool_names = (
        [pool] if isinstance(pool, str) else list(dict.fromkeys(p for p in pool if p))
    )
    if not pool_names:
      raise ValueError("at least one warm pool name is required to open a batch")
    if isinstance(pool, str):
      with self._lock:
        if self._closed:
          raise FleetError("fleet was torn down; call setup() before acquiring again")
        fast = self._live.get(cluster.name)
      if fast is not None and fast.err() is None:
        return fast
    # Under the cluster's lock, so concurrent first acquires open one batch.
    reuse_existing: SandboxBatch | None = None
    with self._cluster_lock(cluster.name):
      with self._lock:
        if self._closed:
          raise FleetError("fleet was torn down; call setup() before acquiring again")
        existing = self._live.get(cluster.name)
        epoch = self._epoch
      if existing is not None:
        error = existing.err()
        if error is None:
          reuse_existing = existing
        else:
          # A stopped batch hands out nothing more, and its Lease may be gone, so
          # its claims are the reaper's. Members already handed out keep working
          # until then, and new acquires go to a fresh batch. It stays open until
          # teardown releases it.
          with self._lock:
            old_wd = self._watchdogs.pop(cluster.name, None)
          if old_wd is not None:
            old_wd.stop()
          logger.warning("batch %s on cluster %s stopped (%s); opening a new one "
                         "for new acquires", existing.batch_id, cluster.name, error)
      if reuse_existing is None:
        s = self._settings
        eff_max_in_flight = s.max_in_flight
        if (
            "max_in_flight" not in s.model_fields_set
            and eff_max_in_flight is not None
        ):
          if not getattr(cluster, "_injected_sync_api_client", False) and callable(
              getattr(cluster, "ensure_pool_maxsize", None)
          ):
            cluster.ensure_pool_maxsize(eff_max_in_flight + BATCH_POOL_HEADROOM + 6)
          sb_c = cluster.sandbox_client
          helper = getattr(sb_c, "k8s_helper", None)
          pool_sz_fn = getattr(helper, "api_pool_size", None)
          pool_sz = pool_sz_fn() if callable(pool_sz_fn) else None
          if (
              isinstance(pool_sz, int)
              and not isinstance(pool_sz, bool)
              and pool_sz > 0
              and eff_max_in_flight + BATCH_POOL_HEADROOM > pool_sz
          ):
            eff_max_in_flight = max(1, pool_sz - BATCH_POOL_HEADROOM)
        else:
          sb_c = cluster.sandbox_client
        batch: SandboxBatch
        batch = sb_c.claim_batch(
            [BatchGroup(warmpool=p, size=0) for p in pool_names],
            namespace=cluster.namespace,
            labels=dict(self._labels),
            create_rps=s.create_rps,
            max_in_flight=eff_max_in_flight,
            work_budget=s.work_budget,
            # Part of each acquired claim's shutdownTime, which expires_at assumes.
            quorum_timeout=self._ready_timeout,
            lease_duration=s.lease_duration)
        stale = False
        registered = False
        wd: BatchWatchdog | None = None
        try:
          with self._lock:
            self._opened.append(batch)
            registered = True
            if self._closed or self._epoch != epoch:
              stale = True
            else:
              self._live[cluster.name] = batch
              if self._fail_fast is not None and self._fail_fast.enabled:
                cluster_name = cluster.name
                wd = BatchWatchdog(
                    cluster, batch, self._fail_fast,
                    record_verdict=lambda cname, v: (
                        self._record_verdict(cluster_name, cname, v)))
                self._watchdogs[cluster.name] = wd
          if not stale and wd is not None:
            wd.start()
        except BaseException:
          if wd is not None:
            wd.stop()
          with self._lock:
            if self._live.get(cluster.name) is batch:
              self._live.pop(cluster.name, None)
            if wd is not None and self._watchdogs.get(cluster.name) is wd:
              self._watchdogs.pop(cluster.name, None)
          try:
            batch.release()
          except Exception:  # noqa: BLE001
            pass
          else:
            if registered:
              with self._lock:
                self._opened = [b for b in self._opened if b is not batch]
          raise
        if stale:
          try:
            batch.release()
          except Exception as exc:  # noqa: BLE001
            logger.warning(
                "batch_claims: release of raced batch on %s failed (kept for retry): %s",
                cluster.name, exc)
          else:
            with self._lock:
              self._opened = [b for b in self._opened if b is not batch]
          raise FleetError(f"fleet has torn down; cannot claim on {cluster.name!r}")
        logger.info("opened batch %s on cluster %s", batch.batch_id, cluster.name)
        return batch
    if not isinstance(pool, str):
      seed_fn = getattr(reuse_existing, "seed_checked_pools", None)
      if verified_ready is not None and callable(seed_fn):
        seed_fn({p: verified_ready[p] for p in pool_names if p in verified_ready})
      elif isinstance(reuse_existing, SandboxBatch):
        reuse_existing.precheck_pools(pool_names, timeout=self._ready_timeout)
    return reuse_existing

  def acquire(
      self,
      cluster: "Cluster",
      pool: str,
      *,
      pod_annotations: dict[str, str] | None = None,
  ) -> BatchClaim:
    """Claims one member from ``pool`` and waits until it is Ready.

    ``pod_annotations`` are forwarded to ``SandboxBatch.acquire`` when set.
    Raises the SDK's ``BatchError`` (including ``TerminalMemberError`` and
    ``BatchTimeoutError``) for anything that happens after the create is
    started, and the pool, template and API errors of ``claim_batch`` otherwise.
    Raises ``FleetError`` once `release_all` has run.
    """
    batch = self._batch_for(cluster, pool)
    started = time.monotonic()
    with self._lock:
      wd = self._watchdogs.get(cluster.name)
      if wd is not None and wd.batch is not batch:
        wd = None
    if wd is not None:
      wd.enter_acquire(started)
    acquired_claim: str | None = None
    try:
      member = batch.acquire(
          pool, timeout=self._ready_timeout, pod_annotations=pod_annotations)
      acquired_claim = member.claim_name
    except BaseException as exc:
      acquired_claim = extract_batch_claim_name(exc)
      raise
    finally:
      if wd is not None:
        wd.exit_acquire(acquired_claim)
    # construct_sandbox_claim_lifecycle_spec formats shutdownTime to whole
    # seconds via strftime, truncating up to almost 1 s of microseconds.
    expires_at = (started + self._ready_timeout + self._settings.work_budget
                  + BATCH_SHUTDOWN_MARGIN_SECONDS - 1.0)
    return BatchClaim(
        batch=batch, member=member, expires_at=expires_at, cluster=cluster)

  def release_all(self) -> dict[int, Exception]:
    """Releases every batch this fleet opened concurrently across clusters.
    Errors are logged and returned keyed by ``id(batch)`` while retaining failed
    batches in ``_opened`` for retry, so one cluster's failure doesn't keep the
    others' claims alive."""
    with self._lock:
      self._closed = True
      self._epoch += 1
      opened = list(self._opened)
      watchdogs = list(self._watchdogs.values())
      self._watchdogs.clear()
      self._verdicts.clear()
      self._live.clear()
    for wd in watchdogs:
      wd.stop()
    if not opened:
      return {}

    failed: dict[int, Exception] = {}
    succeeded_ids: set[int] = set()

    def _release_one(batch: SandboxBatch) -> tuple[int, Exception | None]:
      try:
        batch.release()
      except Exception as exc:  # noqa: BLE001 (best effort, like the teardown sweep)
        logger.warning("failed to release batch %s", batch.batch_id, exc_info=True)
        return id(batch), exc
      return id(batch), None

    if len(opened) == 1:
      bid, err = _release_one(opened[0])
      if err is not None:
        failed[bid] = err
      else:
        succeeded_ids.add(bid)
    else:
      with ThreadPoolExecutor(max_workers=len(opened)) as ex:
        futs = [ex.submit(_release_one, b) for b in opened]
        for fut in futs:
          bid, err = fut.result()
          if err is not None:
            failed[bid] = err
          else:
            succeeded_ids.add(bid)

    if succeeded_ids:
      with self._lock:
        self._opened = [b for b in self._opened if id(b) not in succeeded_ids]
    return failed


@dataclass
class AsyncBatchClaim:
  """One acquired async batch member and the ``AsyncSandboxBatch`` that owns it."""

  batch: Any
  member: Member
  expires_at: float
  cluster: "Cluster" = field(repr=False)
  rebaselined: bool = False
  _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
  _loop_thread: threading.Thread | None = field(default=None, repr=False)

  def still_reaches(self) -> bool:
    if self.rebaselined or self.batch.err() is not None:
      return True
    current = self.batch.get_member(self.member.claim_name)
    return still_reaches(
        current, self.member.sandbox_name, select_pod_ip(self.member.pod_ips))

  async def arelease(self) -> None:
    try:
      await self.batch.release_member(self.member)
    except BatchError:
      helper = getattr(self.cluster, "_async_k8s_helper", None)
      if helper is not None:
        await helper.delete_sandbox_claim(
            self.member.claim_name,
            self.cluster.namespace,
            _request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS,
            log_level=logging.DEBUG,
            batch=True,
        )
      else:
        await asyncio.to_thread(
            self.cluster.resources.delete_claim,
            self.member.claim_name,
            request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS,
        )

  def release(self) -> None:
    if (
        self._loop is not None
        and not self._loop.is_closed()
        and self._loop.is_running()
        and threading.current_thread() is not self._loop_thread
    ):
      asyncio.run_coroutine_threadsafe(self.arelease(), self._loop).result(
          timeout=BATCH_REQUEST_TIMEOUT_SECONDS * 4
      )
      return
    state = getattr(self.batch, "_state", None)
    seq: int | None = None
    try:
      if state is not None:
        state.release_member(self.member.claim_name)
        seq = state.delete_seq(self.member.claim_name)
      wake = getattr(self.batch, "_wake_acquire", None)
      if callable(wake):
        wake(self.member.claim_name)
      connected = getattr(self.batch, "_connected", None)
      if isinstance(connected, dict):
        conn_sb = connected.pop(self.member.claim_name, None)
        close_conn = getattr(conn_sb, "close_connection", None)
        if callable(close_conn):
          res = close_conn()
          if asyncio.iscoroutine(res):
            if (
                self._loop is not None
                and not self._loop.is_closed()
                and self._loop.is_running()
                and threading.current_thread() is self._loop_thread
            ):
              t = self._loop.create_task(res)
              t.add_done_callback(
                  lambda done: None if done.cancelled() else done.exception()
              )
            else:
              res.close()
    except Exception:  # noqa: BLE001
      pass
    self.cluster.resources.delete_claim(
        self.member.claim_name,
        request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS,
    )
    if state is not None and seq is not None:
      try:
        state.note_deleted(self.member.claim_name, seq)
      except Exception:  # noqa: BLE001
        pass


class AsyncBatchClaims:
  """Event-loop-native counterpart to `BatchClaims` using `AsyncSandboxBatch`."""

  def __init__(self, settings: "BatchClaimConfig", *, ready_timeout: int,
               labels: dict[str, str],
               fail_fast: "FailFastPolicy | None" = None) -> None:
    self._settings = settings
    self._ready_timeout = ready_timeout
    self._labels = labels
    self._fail_fast = fail_fast
    self._lock = threading.Lock()
    self._cluster_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
    self._live: dict[str, tuple[Any, asyncio.AbstractEventLoop]] = {}
    self._watchdogs: dict[str, BatchWatchdog] = {}
    self._verdicts: dict[tuple[str, str], StartVerdict] = {}
    self._opened: list[tuple["Cluster", Any, asyncio.AbstractEventLoop, threading.Thread]] = []
    self._closed = False
    self._epoch: int = 0

  def reopen(self) -> None:
    with self._lock:
      self._closed = False

  def mark_closing(self) -> None:
    with self._lock:
      self._closed = True
      self._epoch += 1

  def _record_verdict(
      self, cluster_name: str, claim_name: str, verdict: StartVerdict
  ) -> None:
    with self._lock:
      if (
          (cluster_name, claim_name) not in self._verdicts
          and len(self._verdicts) >= 4096
      ):
        self._verdicts.pop(next(iter(self._verdicts)), None)
      self._verdicts[(cluster_name, claim_name)] = verdict

  def pop_verdict(self, cluster_name: str, claim_name: str) -> StartVerdict | None:
    with self._lock:
      return self._verdicts.pop((cluster_name, claim_name), None)

  def _cluster_lock(self, name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with self._lock:
      cur = self._cluster_locks.get(name)
      if cur is None or cur[0] is not loop:
        cur = (loop, asyncio.Lock())
        self._cluster_locks[name] = cur
      return cur[1]

  def has_live_batch(self, cluster_name: str) -> bool:
    with self._lock:
      entry = self._live.get(cluster_name)
    if entry is None:
      return False
    batch, batch_loop = entry
    if batch_loop.is_closed() or batch.err() is not None:
      return False
    try:
      cur_loop = asyncio.get_running_loop()
    except RuntimeError:
      return True
    return batch_loop is cur_loop

  def has_opened_batches(self) -> bool:
    with self._lock:
      return bool(self._opened)

  async def prepare_clusters(
      self,
      cluster_pools: Sequence[tuple["Cluster", Sequence[str]]],
      *,
      only_unopened: bool = False,
      verified_ready: dict[str, dict[str, int]] | None = None,
  ) -> None:
    pending: list[tuple["Cluster", list[str], dict[str, int] | None]] = []
    for cluster, pools in cluster_pools:
      deduped = list(dict.fromkeys(p for p in pools if p))
      if not deduped:
        continue
      if only_unopened and self.has_live_batch(cluster.name):
        continue
      vmap = verified_ready.get(cluster.name) if verified_ready else None
      pending.append((cluster, deduped, vmap))
    if not pending:
      return
    if len(pending) == 1:
      cluster, deduped, vmap = pending[0]
      await self._batch_for(cluster, deduped, verified_ready=vmap)
      return
    outcomes = await asyncio.gather(
        *(self._batch_for(c, p, verified_ready=vm) for c, p, vm in pending),
        return_exceptions=True,
    )
    first_err = next((o for o in outcomes if isinstance(o, BaseException)), None)
    if first_err is not None:
      raise first_err

  async def _batch_for(
      self,
      cluster: "Cluster",
      pool: str | Sequence[str],
      *,
      verified_ready: dict[str, int] | None = None,
  ):
    pool_names = (
        [pool] if isinstance(pool, str) else list(dict.fromkeys(p for p in pool if p))
    )
    if not pool_names:
      raise ValueError("at least one warm pool name is required to open a batch")
    loop = asyncio.get_running_loop()
    loop_thread = threading.current_thread()
    if isinstance(pool, str):
      with self._lock:
        if self._closed:
          raise FleetError("fleet was torn down; call setup() before acquiring again")
        fast_entry = self._live.get(cluster.name)
      if (
          fast_entry is not None
          and fast_entry[1] is loop
          and not fast_entry[1].is_closed()
          and fast_entry[0].err() is None
      ):
        return fast_entry[0]
    reuse_existing: Any = None
    async with self._cluster_lock(cluster.name):
      with self._lock:
        if self._closed:
          raise FleetError("fleet was torn down; call setup() before acquiring again")
        existing_entry = self._live.get(cluster.name)
        epoch = self._epoch
      if existing_entry is not None:
        existing, existing_loop = existing_entry
        error = existing.err()
        if existing_loop is loop and not existing_loop.is_closed() and error is None:
          reuse_existing = existing
        else:
          with self._lock:
            old_wd = self._watchdogs.pop(cluster.name, None)
          if old_wd is not None:
            old_wd.stop()
          reason = error if (existing_loop is loop and not existing_loop.is_closed()) else "event loop changed"
          logger.warning("async batch %s on cluster %s stopped (%s); opening a new one "
                         "for new acquires", existing.batch_id, cluster.name, reason)
      if reuse_existing is None:
        s = self._settings
        eff_max_in_flight = s.max_in_flight
        if (
            "max_in_flight" not in s.model_fields_set
            and eff_max_in_flight is not None
            and not getattr(cluster, "_injected_async_api_client", False)
            and callable(getattr(cluster, "ensure_pool_maxsize", None))
        ):
          cluster.ensure_pool_maxsize(eff_max_in_flight + BATCH_POOL_HEADROOM + 6)
        sb_client = await cluster.get_async_sandbox_client()
        if (
            "max_in_flight" not in s.model_fields_set
            and eff_max_in_flight is not None
        ):
          helper = getattr(sb_client, "k8s_helper", None)
          pool_sz_fn = getattr(helper, "api_pool_size", None)
          if inspect.iscoroutinefunction(pool_sz_fn):
            pool_sz = await pool_sz_fn()
          elif callable(pool_sz_fn):
            res_sz = pool_sz_fn()
            pool_sz = await res_sz if inspect.isawaitable(res_sz) else res_sz
          else:
            pool_sz = None
          if (
              isinstance(pool_sz, int)
              and not isinstance(pool_sz, bool)
              and pool_sz > 0
              and eff_max_in_flight + BATCH_POOL_HEADROOM > pool_sz
          ):
            eff_max_in_flight = max(1, pool_sz - BATCH_POOL_HEADROOM)
        batch = await sb_client.claim_batch(
            [BatchGroup(warmpool=p, size=0) for p in pool_names],
            namespace=cluster.namespace,
            labels=dict(self._labels),
            create_rps=s.create_rps,
            max_in_flight=eff_max_in_flight,
            work_budget=s.work_budget,
            quorum_timeout=self._ready_timeout,
            lease_duration=s.lease_duration,
        )
        stale = False
        registered = False
        wd: BatchWatchdog | None = None
        try:
          with self._lock:
            self._opened.append((cluster, batch, loop, loop_thread))
            registered = True
            if self._closed or self._epoch != epoch:
              stale = True
            else:
              self._live[cluster.name] = (batch, loop)
              if self._fail_fast is not None and self._fail_fast.enabled:
                cluster_name = cluster.name
                wd = BatchWatchdog(
                    cluster, batch, self._fail_fast,
                    record_verdict=lambda cname, v: (
                        self._record_verdict(cluster_name, cname, v)),
                    loop=loop)
                self._watchdogs[cluster.name] = wd
          if not stale and wd is not None:
            wd.start()
        except BaseException:
          if wd is not None:
            wd.stop()
          with self._lock:
            cur_live = self._live.get(cluster.name)
            if cur_live is not None and cur_live[0] is batch:
              self._live.pop(cluster.name, None)
            if wd is not None and self._watchdogs.get(cluster.name) is wd:
              self._watchdogs.pop(cluster.name, None)
          try:
            await batch.release()
          except Exception:  # noqa: BLE001
            pass
          else:
            if registered:
              with self._lock:
                self._opened = [t for t in self._opened if t[1] is not batch]
          raise
        if stale:
          try:
            await batch.release()
          except Exception as exc:  # noqa: BLE001
            logger.warning(
                "batch_claims: release of raced async batch on %s failed (kept for retry): %s",
                cluster.name, exc)
          else:
            with self._lock:
              self._opened = [t for t in self._opened if t[1] is not batch]
          raise FleetError(f"fleet has torn down; cannot claim on {cluster.name!r}")
        logger.info("opened async batch %s on cluster %s", batch.batch_id, cluster.name)
        return batch
    if not isinstance(pool, str):
      seed_fn = getattr(reuse_existing, "seed_checked_pools", None)
      precheck_fn = getattr(reuse_existing, "precheck_pools", None)
      if verified_ready is not None and inspect.iscoroutinefunction(seed_fn):
        await seed_fn({p: verified_ready[p] for p in pool_names if p in verified_ready})
      elif inspect.iscoroutinefunction(precheck_fn):
        await precheck_fn(pool_names, timeout=self._ready_timeout)
    return reuse_existing

  async def acquire(
      self,
      cluster: "Cluster",
      pool: str,
      *,
      pod_annotations: dict[str, str] | None = None,
  ) -> AsyncBatchClaim:
    batch = await self._batch_for(cluster, pool)
    loop = asyncio.get_running_loop()
    loop_thread = threading.current_thread()
    started = time.monotonic()
    with self._lock:
      wd = self._watchdogs.get(cluster.name)
      if wd is not None and wd.batch is not batch:
        wd = None
    if wd is not None:
      wd.enter_acquire(started)
    acquired_claim: str | None = None
    try:
      member = await batch.acquire(
          pool, timeout=self._ready_timeout, pod_annotations=pod_annotations)
      acquired_claim = member.claim_name
    except BaseException as exc:
      acquired_claim = extract_batch_claim_name(exc)
      raise
    finally:
      if wd is not None:
        wd.exit_acquire(acquired_claim)
    expires_at = (started + self._ready_timeout + self._settings.work_budget
                  + BATCH_SHUTDOWN_MARGIN_SECONDS - 1.0)
    return AsyncBatchClaim(
        batch=batch, member=member, expires_at=expires_at, cluster=cluster,
        _loop=loop, _loop_thread=loop_thread)

  async def release_all(self) -> dict[int, Exception]:
    with self._lock:
      self._closed = True
      self._epoch += 1
      opened = list(self._opened)
      watchdogs = list(self._watchdogs.values())
      self._watchdogs.clear()
      self._verdicts.clear()
      self._live.clear()
    for wd in watchdogs:
      wd.stop()
    if not opened:
      return {}

    failed: dict[int, Exception] = {}
    succeeded_ids: set[int] = set()

    async def _release_one(batch: Any) -> tuple[int, Exception | None]:
      try:
        await batch.release()
      except Exception as exc:  # noqa: BLE001
        logger.warning("failed to release async batch %s", batch.batch_id, exc_info=True)
        return id(batch), exc
      return id(batch), None

    outcomes = await asyncio.gather(*(_release_one(b) for _c, b, _l, _lt in opened))
    for bid, err in outcomes:
      if err is not None:
        failed[bid] = err
      else:
        succeeded_ids.add(bid)

    if succeeded_ids:
      with self._lock:
        self._opened = [t for t in self._opened if id(t[1]) not in succeeded_ids]
    return failed

  def release_all_sync(self) -> dict[int, Exception]:
    """Synchronous teardown backstop (for `atexit` or breaker threads)."""
    with self._lock:
      self._closed = True
      self._epoch += 1
      opened = list(self._opened)
      watchdogs = list(self._watchdogs.values())
      self._watchdogs.clear()
      self._verdicts.clear()
      self._live.clear()
    for wd in watchdogs:
      wd.stop()
    if not opened:
      return {}

    failed: dict[int, Exception] = {}
    succeeded_ids: set[int] = set()
    for cluster, batch, loop, loop_thread in opened:
      if (
          loop is not None
          and not loop.is_closed()
          and loop.is_running()
          and threading.current_thread() is not loop_thread
          and inspect.iscoroutinefunction(getattr(batch, "release", None))
      ):
        try:
          asyncio.run_coroutine_threadsafe(batch.release(), loop).result(
              timeout=BATCH_REQUEST_TIMEOUT_SECONDS * 4
          )
          succeeded_ids.add(id(batch))
        except Exception as exc:  # noqa: BLE001
          logger.warning("failed to release async batch %s on running loop", batch.batch_id, exc_info=True)
          failed[id(batch)] = exc
        continue
      if loop is not None and not loop.is_closed() and loop.is_running():
        def _stop_bg(b=batch):
          b._detached = True
          stop_evt = getattr(b, "_stop_creating", None)
          if stop_evt is not None:
            stop_evt.set()
          for attr in ("_watch_task", "_renew_task"):
            t = getattr(b, attr, None)
            if t is not None and not t.done():
              t.cancel()
        try:
          loop.call_soon_threadsafe(_stop_bg)
        except RuntimeError:
          pass
      try:
        if hasattr(batch, "_lease_name") and hasattr(batch, "_holder_identity"):
          _delete_batch_objects_sync(
              cluster.k8s_helper,
              batch.namespace,
              batch.batch_id,
              batch._lease_name,
              batch._holder_identity,
              batch._lease_duration,
              batch._lease,
          )
          batch._lease_released = True
          batch._released = True
        elif callable(getattr(batch, "release", None)) and not inspect.iscoroutinefunction(batch.release):
          batch.release()
        succeeded_ids.add(id(batch))
      except Exception as exc:  # noqa: BLE001
        logger.warning("failed to sync-release async batch %s", getattr(batch, "batch_id", "?"), exc_info=True)
        failed[id(batch)] = exc
    if succeeded_ids:
      with self._lock:
        self._opened = [t for t in self._opened if id(t[1]) not in succeeded_ids]
    return failed
