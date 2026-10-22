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

"""Claims sandboxes through SDK batches for ``FleetConfig(claim_mode="batch")``.

Each fleet holds one SDK batch per cluster. Each `SandboxFleet.acquire` adds one
member to that batch with `SandboxBatch.acquire`. The batch's one watch reports
every member's readiness, so the fleet opens no watch per claim. The batch Lease
owns the claims, so the batch reaper deletes them when a driver dies without
tearing down.

The first acquire on a cluster opens the batch with one size-0 group for that
acquire's pool. ``claim_batch`` checks every group's pool and template one after
another before it returns. Opening the batch with every planned pool would put two
GETs per image on the first acquire's thread. Instead, `SandboxBatch.acquire`
checks each later pool once, on the thread of the first acquire that uses it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from k8s_agent_sandbox import BatchGroup

from .exceptions import FleetError

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

  def release(self) -> None:
    self.batch.release_member(self.member)


class BatchClaims:
  """Holds the batches one fleet claims through, with one live batch per cluster."""

  def __init__(self, settings: "BatchClaimConfig", *, ready_timeout: int,
               labels: dict[str, str]):
    self._settings = settings
    self._ready_timeout = ready_timeout
    self._labels = labels
    self._lock = threading.Lock()
    self._cluster_locks: dict[str, threading.Lock] = {}
    self._live: dict[str, "SandboxBatch"] = {}
    # Every batch this fleet opened, including batches it replaced after an
    # error. release_all releases each one, which deletes its remaining claims
    # and its Lease.
    self._opened: list["SandboxBatch"] = []
    # release_all sets this, so an acquire that races teardown can't open a new
    # batch. No later teardown would release that batch, and its Lease would keep
    # renewing.
    self._closed = False

  def reopen(self) -> None:
    """Lets acquires open batches again after `release_all`. `SandboxFleet.setup`
    calls it."""
    with self._lock:
      self._closed = False

  def _cluster_lock(self, name: str) -> threading.Lock:
    with self._lock:
      return self._cluster_locks.setdefault(name, threading.Lock())

  def _batch_for(self, cluster: "Cluster", pool: str) -> "SandboxBatch":
    # The cluster's lock makes concurrent first acquires open only one batch.
    with self._cluster_lock(cluster.name):
      with self._lock:
        if self._closed:
          raise FleetError("fleet was torn down; call setup() before acquiring again")
        batch = self._live.get(cluster.name)
      if batch is not None:
        error = batch.err()
        if error is None:
          return batch
        # A stopped batch hands out no new members. Its Lease may be gone, and
        # then the batch reaper deletes its claims. Members it already handed
        # out keep working until that happens. New acquires go to a fresh batch,
        # and teardown still releases the stopped one.
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
          # Part of each acquired claim's shutdownTime.
          quorum_timeout=self._ready_timeout,
          lease_duration=s.lease_duration)
      with self._lock:
        closed = self._closed
        if not closed:
          self._live[cluster.name] = batch
          self._opened.append(batch)
      if closed:
        # Teardown ran while this batch was opening and never saw it, so release
        # it here.
        batch.release()
        raise FleetError("fleet was torn down; call setup() before acquiring again")
      logger.info("opened batch %s on cluster %s", batch.batch_id, cluster.name)
      return batch

  def acquire(self, cluster: "Cluster", pool: str) -> BatchClaim:
    """Claims one member from ``pool`` and waits until it is Ready.

    A failure after the create starts raises the SDK's ``BatchError``, which
    covers ``TerminalMemberError`` and ``BatchTimeoutError``. A failure before it
    raises the pool, template, and API errors of ``claim_batch``. After
    `release_all`, it raises ``FleetError``.
    """
    batch = self._batch_for(cluster, pool)
    member = batch.acquire(pool, timeout=self._ready_timeout)
    return BatchClaim(batch=batch, member=member)

  def release_all(self) -> None:
    """Releases every batch this fleet opened. It logs a failed release and goes
    on, so one cluster's failure doesn't keep the other clusters' claims alive."""
    with self._lock:
      self._closed = True
      opened, self._opened = self._opened, []
      self._live.clear()
    for batch in opened:
      try:
        batch.release()
      except Exception:  # noqa: BLE001 (best effort, like the teardown sweep)
        logger.warning("failed to release batch %s", batch.batch_id, exc_info=True)
