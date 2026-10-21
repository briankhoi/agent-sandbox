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

The batch is opened by the first acquire on its cluster, from that acquire's pool
as a size-0 group. ``claim_batch`` checks every group's pool and template one
after another before it returns, so opening it with every planned pool would put
two GETs per image on the first acquire's thread; `SandboxBatch.acquire` checks
each later pool once, on the thread of its own first acquire.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from k8s_agent_sandbox import BatchGroup
from k8s_agent_sandbox.batch_utils import BATCH_SHUTDOWN_MARGIN_SECONDS

if TYPE_CHECKING:
  from k8s_agent_sandbox import Member, SandboxBatch

  from .cluster import Cluster
  from .config import BatchClaimConfig

logger = logging.getLogger("agent_sandbox_rl.batch_claims")


@dataclass
class BatchClaim:
  """One acquired batch member and the batch that owns it."""

  batch: "SandboxBatch"
  member: "Member"
  # time.monotonic() by which the controller deletes the claim (its shutdownTime),
  # measured from before the create, so it is never later than the real one.
  expires_at: float

  def release(self) -> None:
    self.batch.release_member(self.member)


class BatchClaims:
  """The batches one fleet claims through, one live batch per cluster."""

  def __init__(self, settings: "BatchClaimConfig", *, ready_timeout: int,
               labels: dict[str, str]):
    self._settings = settings
    self._ready_timeout = ready_timeout
    self._labels = labels
    self._lock = threading.Lock()
    self._cluster_locks: dict[str, threading.Lock] = {}
    self._live: dict[str, "SandboxBatch"] = {}
    # Every batch opened, including ones replaced after an error, so teardown
    # releases their remaining claims and Leases.
    self._opened: list["SandboxBatch"] = []

  def _cluster_lock(self, name: str) -> threading.Lock:
    with self._lock:
      return self._cluster_locks.setdefault(name, threading.Lock())

  def _batch_for(self, cluster: "Cluster", pool: str) -> "SandboxBatch":
    # Under the cluster's lock, so concurrent first acquires open one batch.
    with self._cluster_lock(cluster.name):
      batch = self._live.get(cluster.name)
      if batch is not None:
        error = batch.err()
        if error is None:
          return batch
        # A stopped batch hands out nothing more, and its Lease may be gone, so
        # its claims are the reaper's. Members already handed out keep working
        # until then; new acquires go to a fresh batch.
        logger.warning("batch %s on cluster %s stopped (%s); opening a new one "
                       "for new acquires", batch.batch_id, cluster.name, error)
      s = self._settings
      batch = cluster.batch_sandbox_client().claim_batch(
          [BatchGroup(warmpool=pool, size=0)],
          namespace=cluster.namespace,
          labels=dict(self._labels),
          create_rps=s.create_rps,
          max_in_flight=s.max_in_flight,
          work_budget=s.work_budget,
          # Nothing waits for a quorum here, and acquire's wait defaults to this.
          quorum_timeout=self._ready_timeout,
          lease_duration=s.lease_duration)
      logger.info("opened batch %s on cluster %s", batch.batch_id, cluster.name)
      self._live[cluster.name] = batch
      with self._lock:
        self._opened.append(batch)
      return batch

  def acquire(self, cluster: "Cluster", pool: str) -> BatchClaim:
    """Claims one member from ``pool`` and waits until it is Ready.

    Raises the SDK's errors: a ``BatchError`` (including ``TerminalMemberError``
    and ``BatchTimeoutError``) for anything that happens after the create is
    started, and the pool/template/API errors of ``claim_batch`` otherwise.
    """
    batch = self._batch_for(cluster, pool)
    started = time.monotonic()
    member = batch.acquire(pool, timeout=self._ready_timeout)
    expires_at = (started + self._ready_timeout + self._settings.work_budget
                  + BATCH_SHUTDOWN_MARGIN_SECONDS)
    return BatchClaim(batch=batch, member=member, expires_at=expires_at)

  def release_all(self) -> None:
    """Releases every batch this fleet opened. Errors are logged, so one cluster's
    failure doesn't keep the others' claims alive."""
    with self._lock:
      opened, self._opened = self._opened, []
    self._live.clear()
    for batch in opened:
      try:
        batch.release()
      except Exception:  # noqa: BLE001 — best-effort, like the teardown sweep
        logger.warning("failed to release batch %s", batch.batch_id, exc_info=True)
