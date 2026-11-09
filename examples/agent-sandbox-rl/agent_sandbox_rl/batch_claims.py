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

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from k8s_agent_sandbox import BatchError, BatchGroup, Member, SandboxBatch
from k8s_agent_sandbox.batch_state import still_reaches
from k8s_agent_sandbox.batch_utils import (
    BATCH_REQUEST_TIMEOUT_SECONDS,
    BATCH_SHUTDOWN_MARGIN_SECONDS,
)
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
  ) -> None:
    """Pre-open each cluster's batch with its warmed ``pools`` as size-0 groups."""
    pending: list[tuple["Cluster", list[str]]] = []
    for cluster, pools in cluster_pools:
      deduped = list(dict.fromkeys(p for p in pools if p))
      if not deduped:
        continue
      if only_unopened and self.has_live_batch(cluster.name):
        continue
      pending.append((cluster, deduped))
    if not pending:
      return
    if len(pending) == 1:
      cluster, deduped = pending[0]
      self._batch_for(cluster, deduped)
      return
    first_err: BaseException | None = None
    with ThreadPoolExecutor(max_workers=len(pending)) as ex:
      futs = [ex.submit(self._batch_for, c, p) for c, p in pending]
      for fut in futs:
        try:
          fut.result()
        except BaseException as exc:  # noqa: BLE001 (drain remaining clusters first)
          if first_err is None:
            first_err = exc
    if first_err is not None:
      raise first_err

  def _batch_for(self, cluster: "Cluster", pool: str | Sequence[str]) -> SandboxBatch:
    pool_names = (
        [pool] if isinstance(pool, str) else list(dict.fromkeys(p for p in pool if p))
    )
    if not pool_names:
      raise ValueError("at least one warm pool name is required to open a batch")
    # Under the cluster's lock, so concurrent first acquires open one batch.
    with self._cluster_lock(cluster.name):
      with self._lock:
        if self._closed:
          raise FleetError("fleet was torn down; call setup() before acquiring again")
        existing = self._live.get(cluster.name)
        epoch = self._epoch
      if existing is not None:
        error = existing.err()
        if error is None:
          return existing
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
      s = self._settings
      batch: SandboxBatch
      batch = cluster.sandbox_client.claim_batch(
          [BatchGroup(warmpool=p, size=0) for p in pool_names],
          namespace=cluster.namespace,
          labels=dict(self._labels),
          create_rps=s.create_rps,
          max_in_flight=s.max_in_flight,
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
