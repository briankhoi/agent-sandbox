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

"""Tests for FleetConfig(claim_mode="batch"): acquire/release/teardown through one
SDK batch per cluster, against a fake batch."""

import threading
import time
import types
from unittest.mock import MagicMock

import pytest
from k8s_agent_sandbox import BatchError, Member, MemberState
from k8s_agent_sandbox.exceptions import SandboxNotReadyError
from kubernetes import client
from pydantic import ValidationError

from agent_sandbox_rl import (AsyncSandboxFleet, BatchClaimConfig,
                              ClusterRegistry, FailFastPolicy, FleetConfig,
                              SandboxFleet, SandboxStartError, constants,
                              observability, reuse_git_restore_sandbox,
                              reuse_git_restore_sandbox_async)
from agent_sandbox_rl.batch_claims import BatchClaims
from agent_sandbox_rl.exceptions import FleetError
from agent_sandbox_rl.handles import SandboxHandle
from agent_sandbox_rl.sources import Task

_CLEAN = "PRISTINE=abc\nHEAD=abc\nDIRTY=0\nENV=e1\nCFG=g1\nPROCS=5"


class FakeBatch:
  """Stands in for SandboxBatch: acquire hands out Ready members at once."""

  def __init__(self, cluster, seq):
    self.batch_id = f"b-{cluster}-{seq}"
    self._cluster = cluster
    self._n = 0
    self._lock = threading.Lock()
    self._members: dict[str, Member] = {}
    self._waiters: dict[str, threading.Event] = {}
    self._acquire_waiters = self._waiters
    self.error = None
    self.acquired = []
    self.acquired_annotations = []
    self.connected = []
    self.released_members = []
    self.released = False
    self.acquire_error = None
    self.hold_pending = False
    self.warm_delay_s = 0.0

  def err(self):
    return self.error

  def _check_healthy(self):
    if self.released:
      raise BatchError(f"batch '{self.batch_id}' has been detached or released")
    if self.error is not None:
      raise BatchError(f"batch '{self.batch_id}' has stopped") from self.error

  def get_member(self, claim_name: str) -> Member | None:
    with self._lock:
      return self._members.get(claim_name)

  def members(self, warmpool=None):
    with self._lock:
      return [m for m in self._members.values()
              if warmpool is None or m.warmpool == warmpool]

  def acquire(self, warmpool, timeout=None, *, pod_annotations=None):
    self._check_healthy()
    if self.acquire_error is not None:
      raise self.acquire_error
    with self._lock:
      self._n += 1
      n = self._n
      claim_name = f"{self.batch_id}-{n}"
      sandbox_name = f"sb-{self.batch_id}-{n}"
      if self.hold_pending or self.warm_delay_s > 0:
        m = Member(claim_name=claim_name, sandbox_name=sandbox_name,
                   warmpool=warmpool, pod_ips=(), state=MemberState.PENDING)
        ev = threading.Event()
        self._waiters[claim_name] = ev
      else:
        m = Member(claim_name=claim_name, sandbox_name=sandbox_name,
                   warmpool=warmpool, pod_ips=("10.1.0.5",), state=MemberState.READY)
        ev = None
      self._members[claim_name] = m
    self.acquired.append((warmpool, timeout))
    self.acquired_annotations.append(pod_annotations)
    if ev is not None:
      if self.warm_delay_s > 0 and not self.hold_pending:
        ev.wait(self.warm_delay_s)
        with self._lock:
          self._waiters.pop(claim_name, None)
          if claim_name in self.released_members:
            raise BatchError(
                f"acquired member '{claim_name}' was released before it was Ready")
          m = Member(claim_name=claim_name, sandbox_name=sandbox_name,
                     warmpool=warmpool, pod_ips=("10.1.0.5",), state=MemberState.READY)
          self._members[claim_name] = m
      else:
        woke = ev.wait(timeout if timeout is not None else 5.0)
        with self._lock:
          self._waiters.pop(claim_name, None)
        if claim_name in self.released_members:
          raise BatchError(
              f"acquired member '{claim_name}' was released before it was Ready")
        if not woke:
          raise BatchError(f"timed out waiting for {claim_name}")
    return m

  def connect(self, member):
    self._check_healthy()
    s = MagicMock()
    s.claim_name = member.claim_name
    s.sandbox_id = member.sandbox_name
    self.connected.append(s)
    return s

  def release_member(self, member):
    self._check_healthy()
    with self._lock:
      self._members.pop(member.claim_name, None)
      self.released_members.append(member.claim_name)
      ev = self._waiters.pop(member.claim_name, None)
    if ev is not None:
      ev.set()

  def release(self):
    with self._lock:
      self.released = True
      waiters = list(self._waiters.values())
      self._waiters.clear()
    for ev in waiters:
      ev.set()


def _batch_cluster(make_cluster, name="solo"):
  c = make_cluster(name)
  c.batches = []

  def claim_batch(groups, **kwargs):
    b = FakeBatch(name, len(c.batches))
    b.groups = list(groups)
    b.kwargs = kwargs
    c.batches.append(b)
    return b
  c.sandbox_client.claim_batch.side_effect = claim_batch
  return c


def _fleet(registry, **cfg):
  return SandboxFleet(FleetConfig(claim_mode="batch", **cfg), registry=registry)


def test_claim_mode_defaults_to_per_claim_and_rejects_unknown():
  assert FleetConfig().claim_mode == "per_claim"
  with pytest.raises(ValidationError, match="claim_mode"):
    FleetConfig(claim_mode="bulk")
  # Values the SDK would reject at the first acquire fail here instead.
  for bad in (dict(lease_duration=5), dict(work_budget=0), dict(create_rps=0),
              dict(max_in_flight=0)):
    with pytest.raises(ValidationError):
      BatchClaimConfig(**bad)
  BatchClaimConfig(lease_duration=6, work_budget=1, create_rps=0.5, max_in_flight=1)


def test_batch_claims_retains_failed_batch_across_teardown_race_and_retries():
  bc = BatchClaims(BatchClaimConfig(), ready_timeout=30, labels={"run": "r1"})
  cluster = MagicMock()
  cluster.name = "c1"
  cluster.namespace = "default"

  raced = MagicMock(batch_id="b-raced")
  raced.release.side_effect = [client.ApiException(status=503), None]

  def claim_while_closing(*_args, **_kwargs):
    bc.release_all()
    return raced

  cluster.sandbox_client.claim_batch.side_effect = claim_while_closing
  with pytest.raises(FleetError):
    bc.acquire(cluster, "pool-a")
  assert raced.release.call_count == 1

  # A follow-up release_all retries the batch whose first release failed.
  bc.release_all()
  assert raced.release.call_count == 2
  bc.release_all()
  assert raced.release.call_count == 2


def test_batch_claims_reopen_epoch_rejects_stale_opener_without_clobbering_new_epoch():
  bc = BatchClaims(BatchClaimConfig(), ready_timeout=30, labels={"run": "r1"})
  c1 = MagicMock()
  c1.name = "c1"
  c1.namespace = "default"
  c2 = MagicMock()
  c2.name = "c2"
  c2.namespace = "default"

  stale_batch = MagicMock(batch_id="b-c1-stale")
  fresh_c1 = MagicMock(batch_id="b-c1-fresh")
  fresh_c1.err.return_value = None
  fresh_c1.acquire.return_value = Member(
      claim_name="b-c1-fresh-0", sandbox_name="sb-c1", warmpool="pool-a",
      pod_ips=("10.0.0.3",), state=MemberState.READY)
  fresh_c2 = MagicMock(batch_id="b-c2-fresh")
  fresh_c2.err.return_value = None
  fresh_c2.acquire.return_value = Member(
      claim_name="b-c2-fresh-0", sandbox_name="sb-c2", warmpool="pool-b",
      pod_ips=("10.0.0.4",), state=MemberState.READY)
  c2.sandbox_client.claim_batch.return_value = fresh_c2

  calls = 0

  def claim_across_reopen(*_args, **_kwargs):
    nonlocal calls
    calls += 1
    if calls == 1:
      bc.release_all()
      bc.reopen()
      bc.acquire(c2, "pool-b")
      return stale_batch
    return fresh_c1

  c1.sandbox_client.claim_batch.side_effect = claim_across_reopen

  with pytest.raises(FleetError, match="torn down"):
    bc.acquire(c1, "pool-a")

  stale_batch.release.assert_called_once_with()
  fresh_c2.release.assert_not_called()
  assert bc._closed is False
  assert bc._live.get("c2") is fresh_c2
  assert "c1" not in bc._live
  assert stale_batch not in bc._opened

  claimed = bc.acquire(c1, "pool-a")
  assert claimed.batch is fresh_c1
  assert bc._live.get("c1") is fresh_c1


def test_batch_claims_release_all_retains_failed_batches_for_retry():
  bc = BatchClaims(BatchClaimConfig(), ready_timeout=30, labels={"run": "r1"})
  cluster = MagicMock()
  cluster.name = "c1"
  cluster.namespace = "default"

  flaky = MagicMock(batch_id="b-flaky")
  flaky.err.return_value = None
  flaky.acquire.return_value = Member(
      claim_name="b-flaky-1", sandbox_name="sb-1", warmpool="pool-a",
      pod_ips=("10.0.0.2",), state=MemberState.READY)
  flaky.release.side_effect = [client.ApiException(status=503), None]
  cluster.sandbox_client.claim_batch.return_value = flaky

  bc.acquire(cluster, "pool-a")
  bc.release_all()
  assert flaky.release.call_count == 1
  bc.release_all()
  assert flaky.release.call_count == 2


def test_first_acquire_opens_one_batch_from_its_pool(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), ready_timeout=120,
             batch_claims=BatchClaimConfig(create_rps=200, max_in_flight=40))
  tasks = f.load_tasks(["imgA", "imgB", "imgA"])
  f.plan()
  handles = [f.acquire(t) for t in tasks]

  assert len(c.batches) == 1
  b = c.batches[0]
  pool_a = f.plan_.for_image("imgA").pool
  # One size-0 group, from the first acquire's pool only, because claim_batch
  # prechecks every group serially and acquire checks the rest lazily.
  assert [(g.warmpool, g.size) for g in b.groups] == [(pool_a, 0)]
  assert b.kwargs["quorum_timeout"] == 120
  assert b.kwargs["lease_duration"] == 300
  assert b.kwargs["work_budget"] == 3600
  assert b.kwargs["create_rps"] == 200 and b.kwargs["max_in_flight"] == 40
  assert b.kwargs["labels"][constants.RUN_ID_LABEL] == f.run_id
  assert b.kwargs["namespace"] == c.namespace
  assert [p for p, _ in b.acquired] == [pool_a, f.plan_.for_image("imgB").pool, pool_a]
  assert all(timeout == 120 for _, timeout in b.acquired)
  assert b.acquired_annotations == [None, None, None]
  c.sandbox_client.create_sandbox.assert_not_called()

  h = handles[0]
  # Identity comes from the member, with no Sandbox or pod GETs.
  assert h.pod_name == h.sandbox_id == f"sb-{b.batch_id}-1"
  assert h.claim_name == f"{b.batch_id}-1"
  assert h.pod_ip == "10.1.0.5"
  assert h.sandbox.claim_name == h.claim_name     # batch.connect(member)
  assert h.claim_expires_at is not None
  assert c.active_claims == 3


def test_claim_timeout_overrides_ready_timeout_for_batch_claims(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), claim_timeout=120, ready_timeout=900)
  t = f.load_tasks(["img"])[0]
  f.plan()
  f.acquire(t)
  b = c.batches[0]
  assert b.kwargs["quorum_timeout"] == 120
  assert b.acquired == [(f.plan_.for_image("img").pool, 120)]


def test_concurrent_first_acquires_open_one_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  open_batch = c.sandbox_client.claim_batch.side_effect

  def slow_open(groups, **kwargs):
    time.sleep(0.05)                        # so the first acquires overlap the open
    return open_batch(groups, **kwargs)
  c.sandbox_client.claim_batch.side_effect = slow_open
  f = _fleet(ClusterRegistry([c]), max_concurrent=16)
  tasks = f.load_tasks(["img"] * 16)
  f.plan()
  out = []
  threads = [threading.Thread(target=lambda t=t: out.append(f.acquire(t))) for t in tasks]
  for th in threads:
    th.start()
  for th in threads:
    th.join()
  assert len(out) == 16 and len(c.batches) == 1


def test_release_goes_through_the_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  t = f.load_tasks(["img"])[0]
  f.plan()
  h = f.acquire(t)
  f.release(h)
  b = c.batches[0]
  assert b.released_members == [h.claim_name]
  c.resources.delete_claim.assert_not_called()
  h.sandbox.terminate.assert_not_called()   # would delete the claim behind the batch
  assert c.active_claims == 0 and f._claims_reserved == 0


def test_teardown_releases_every_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img", "img"])
  f.plan()
  h0 = f.acquire(f.tasks[0])
  c.batches[0].error = BatchError("lease lost")
  h1 = f.acquire(f.tasks[1])                     # the stopped batch is replaced
  assert len(c.batches) == 2
  f.teardown()
  assert c.batches[0].released_members == []
  c.resources.delete_claim.assert_called_once_with(h0.claim_name)
  assert c.batches[1].released_members == [h1.claim_name]
  assert all(b.released for b in c.batches)
  assert c.active_claims == 0 and f._claims_reserved == 0


def test_teardown_releases_batches_when_a_handle_release_fails(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img"])
  f.plan()
  f.acquire(f.tasks[0])
  b = c.batches[0]
  b.release_member = MagicMock(side_effect=RuntimeError("forbidden"))
  with pytest.raises(RuntimeError):
    f.teardown()
  assert b.released


def test_acquire_racing_teardown_leaves_no_batch_open(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img"] * 3)
  f.plan()
  # Teardown runs while a batch is opening, so it can't see that batch.
  open_batch = c.sandbox_client.claim_batch.side_effect

  def open_during_teardown(groups, **kwargs):
    batch = open_batch(groups, **kwargs)
    f.teardown()
    return batch
  c.sandbox_client.claim_batch.side_effect = open_during_teardown
  with pytest.raises(FleetError):
    f.acquire(f.tasks[0])
  assert [b.released for b in c.batches] == [True]
  # After teardown, an acquire opens nothing.
  c.sandbox_client.claim_batch.side_effect = open_batch
  with pytest.raises(FleetError):
    f.acquire(f.tasks[1])
  assert len(c.batches) == 1 and f._claims_reserved == 0


def test_claim_landing_after_teardown_falls_back_to_direct_claim_delete(make_cluster):
  # Window 1 runs teardown inside batch.acquire before batch.connect, so connect
  # raises BatchError on the released batch and rollback deletes the claim directly.
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img", "img"])
  f.plan()
  h0 = f.acquire(f.tasks[0])
  b = c.batches[0]
  claim_member = b.acquire

  def acquire_during_teardown(warmpool, timeout=None, *, pod_annotations=None):
    member = claim_member(warmpool, timeout, pod_annotations=pod_annotations)
    f.teardown()
    return member
  b.acquire = acquire_during_teardown
  with pytest.raises(BatchError):
    f.acquire(f.tasks[1])
  assert b.released_members == [h0.claim_name]
  c.resources.delete_claim.assert_called_once_with(f"{b.batch_id}-2")
  assert f._claims_reserved == 0

  # Window 2 runs teardown after batch.connect returns, so fleet.acquire sees
  # _torndown, deletes the claim directly once release_member rejects the released
  # batch, and raises FleetError without calling sandbox.terminate().
  c2 = _batch_cluster(make_cluster)
  f2 = _fleet(ClusterRegistry([c2]))
  f2.load_tasks(["img", "img"])
  f2.plan()
  h2 = f2.acquire(f2.tasks[0])
  b2 = c2.batches[0]
  connect_member = b2.connect

  def connect_during_teardown(member):
    sandbox = connect_member(member)
    f2.teardown()
    return sandbox
  b2.connect = connect_during_teardown
  with pytest.raises(FleetError):
    f2.acquire(f2.tasks[1])
  assert b2.released_members == [h2.claim_name]
  c2.resources.delete_claim.assert_called_once_with(f"{b2.batch_id}-2")
  assert not b2.connected[-1].terminate.called
  assert f2._claims_reserved == 0


def test_setup_after_teardown_opens_a_fresh_batch(make_cluster, monkeypatch):
  monkeypatch.setattr("agent_sandbox_rl.preflight.preflight_cluster",
                      lambda cluster, **kw: MagicMock(ok=True, checks=[]))
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img"])
  f.setup()
  f.acquire(f.tasks[0])
  f.teardown()
  f.setup()
  h = f.acquire(f.tasks[0])
  assert len(c.batches) == 2 and h.claim_name.startswith(c.batches[1].batch_id)
  f.teardown()


def test_stopped_batch_is_replaced_and_old_members_still_release(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img", "img"])
  f.plan()
  old = f.acquire(f.tasks[0])
  c.batches[0].error = BatchError("lease lost")
  new = f.acquire(f.tasks[1])
  assert new.claim_name.startswith(c.batches[1].batch_id)
  # When the direct fallback delete fails, fleet.release keeps the handle and
  # its reserved slot so the caller can retry.
  c.resources.delete_claim.side_effect = client.ApiException(status=500)
  with pytest.raises(client.ApiException):
    f.release(old)
  assert old in f.handles() and c.active_claims == 2 and f._claims_reserved == 2
  c.resources.delete_claim.side_effect = None
  f.release(old)
  assert c.batches[0].released_members == []
  c.resources.delete_claim.assert_called_with(old.claim_name)
  assert c.active_claims == 1 and f._claims_reserved == 1


def test_failure_after_acquire_releases_the_member(make_cluster, monkeypatch):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img", "img"])
  f.plan()
  f.acquire(f.tasks[0])
  b = c.batches[0]
  monkeypatch.setattr(b, "connect", MagicMock(side_effect=BatchError("stopped")))
  with pytest.raises(BatchError):
    f.acquire(f.tasks[1])
  assert b.released_members == [f"{b.batch_id}-2"]
  assert f._claims_reserved == 1

  monkeypatch.setattr(
      b, "connect", MagicMock(side_effect=SandboxNotReadyError("not ready")))
  with pytest.raises(BatchError) as ei:
    f.acquire(f.tasks[1])
  assert isinstance(ei.value.__cause__, SandboxNotReadyError)
  assert b.released_members == [f"{b.batch_id}-2", f"{b.batch_id}-3"]
  assert f._claims_reserved == 1


def test_annotated_and_pinned_acquires_go_through_the_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  t = f.load_tasks(["img"])[0]
  f.plan()
  h = f.acquire(t, pod_annotations={"a": "b"}, snapshot_uid="snap-1")
  c.sandbox_client.create_sandbox.assert_not_called()
  b = c.batches[0]
  assert b.acquired_annotations == [
      {"a": "b", constants.PODSNAPSHOT_NAME_ANNOTATION: "snap-1"}]
  assert h.pending_snapshot_uid == "snap-1"
  assert h.claim_expires_at is not None
  f.release(h)
  assert b.released_members == [h.claim_name]
  h.sandbox.terminate.assert_not_called()


def _running_pod(name="p", uid="u-1", restarts=0):
  return {"metadata": {"name": name, "uid": uid},
          "status": {"phase": "Running", "container_statuses": [
              {"name": "agent-runtime", "restart_count": restarts,
               "state": {"running": {}}}]}}


def test_batch_acquire_skips_pod_get_and_is_alive_checks_the_batch_watch(make_cluster):
  c = _batch_cluster(make_cluster)
  read_pod = c.core_api.read_namespaced_pod_status
  f = _fleet(ClusterRegistry([c]))
  t = f.load_tasks(["img"])[0]
  f.plan()

  h = f.acquire(t)
  b = c.batches[0]
  pool = f.plan_.for_image("img").pool
  assert read_pod.call_count == 0 and h.pod_uid is None

  # When the watched member still matches, is_alive() checks the pod status.
  read_pod.return_value = _running_pod(name=h.pod_name, uid="u-1")
  assert h.is_alive() is True and read_pod.call_count == 1

  # Claim loss or pod IP replacement on a healthy batch returns False with 0 GETs.
  b._members[h.claim_name] = Member(
      claim_name=h.claim_name, sandbox_name=h.sandbox_id, warmpool=pool,
      pod_ips=(), state=MemberState.LOST)
  assert h.is_alive() is False and read_pod.call_count == 1
  b._members[h.claim_name] = Member(
      claim_name=h.claim_name, sandbox_name=h.sandbox_id, warmpool=pool,
      pod_ips=("10.1.0.9",), state=MemberState.READY)
  assert h.is_alive() is False and read_pod.call_count == 1

  # Once batch.err() is set, the frozen watch is skipped and the pod is checked.
  b.error = BatchError("lease lost")
  assert h.is_alive() is True and read_pod.call_count == 2
  b.error = None

  # refresh() after resume/restore re-baselines the handle even if the best-effort
  # pod status read inside refresh() fails, so the pre-resume IP never rejects the
  # resumed pod.
  h.sandbox.get_pod_name.return_value = h.sandbox_id
  h.sandbox.get_pod_ip.return_value = "10.1.0.9"
  read_pod.side_effect = client.ApiException(status=500)
  h.refresh()
  assert h.pod_uid is None and h._batch_claim.rebaselined is True
  assert read_pod.call_count == 3
  read_pod.side_effect = None
  read_pod.return_value = _running_pod(name=h.pod_name, uid="u-2")
  assert h.is_alive() is True and read_pod.call_count == 4

  # Explicit record_pod_identity() tracks UID and container restarts directly.
  h2 = f.acquire(t)
  read_pod.return_value = _running_pod(name=h2.pod_name, uid="u-7", restarts=0)
  h2.record_pod_identity()
  assert (h2.pod_uid, h2.runtime_restarts) == ("u-7", 0) and read_pod.call_count == 5
  b._members[h2.claim_name] = Member(
      claim_name=h2.claim_name, sandbox_name=h2.sandbox_id, warmpool=pool,
      pod_ips=("10.1.0.9",), state=MemberState.READY)
  assert h2.is_alive() is True and read_pod.call_count == 6
  read_pod.return_value = _running_pod(name=h2.pod_name, uid="u-7", restarts=1)
  assert h2.is_alive() is False and read_pod.call_count == 7


def test_per_claim_mode_opens_no_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  f = SandboxFleet(FleetConfig(), registry=ClusterRegistry([c]))
  t = f.load_tasks(["img"])[0]
  f.plan()
  h = f.acquire(t)
  assert c.batches == [] and h.claim_expires_at is None
  c.sandbox_client.create_sandbox.assert_called_once()


async def test_async_fleet_claims_releases_and_tears_down_through_one_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  af = AsyncSandboxFleet(FleetConfig(claim_mode="batch", max_concurrent=16),
                         registry=ClusterRegistry([c]))
  af.load_tasks(["img"] * 16)
  af._fleet.plan()
  handles = await af.acquire_batch(af.tasks)
  assert len(c.batches) == 1
  b = c.batches[0]
  assert len(b.acquired) == 16
  pinned = await af.acquire(
      af.tasks[0], pod_annotations={"a": "b"}, snapshot_uid="snap-1")
  assert b.acquired_annotations[-1] == {
      "a": "b", constants.PODSNAPSHOT_NAME_ANNOTATION: "snap-1"}
  c.sandbox_client.create_sandbox.assert_not_called()
  await af.release(handles[0])
  await af.release(pinned)
  assert b.released_members == [handles[0].claim_name, pinned.claim_name]
  await af.teardown()
  assert b.released
  af.close()


def test_batch_refresh_reconnects_closed_sandbox_handle(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  t = f.load_tasks(["img"])[0]
  f.plan()
  h = f.acquire(t)
  b = c.batches[0]

  old_sb = h.sandbox
  old_sb.is_active = False
  old_sb._pod_name = "stale-pod"
  old_sb._seeded_pod_ip = "10.1.0.5"
  old_sb._snapshots = types.SimpleNamespace(created_manual_triggers=["trig-1"])
  b._members[h.claim_name] = Member(
      claim_name=h.claim_name, sandbox_name=h.sandbox_id,
      warmpool=f.plan_.for_image("img").pool,
      pod_ips=("10.1.0.9",), state=MemberState.READY)
  reconnected = MagicMock(claim_name=h.claim_name, sandbox_id=h.sandbox_id, is_active=True)
  reconnected._pod_name = "stale-reconnected-pod"
  reconnected._seeded_pod_ip = "10.1.0.9"
  reconnected._snapshots = types.SimpleNamespace(created_manual_triggers=[])
  reconnected.get_pod_name.return_value = "pod-resumed"
  reconnected.get_pod_ip.return_value = "10.1.0.9"
  b.connect = MagicMock(return_value=reconnected)
  c.core_api.read_namespaced_pod_status.return_value = _running_pod(
      name="pod-resumed", uid="u-9")

  h.refresh()
  assert h.sandbox is reconnected and h.sandbox.is_active is True
  assert "_pod_name" not in vars(reconnected)
  assert "_seeded_pod_ip" not in vars(reconnected)
  assert h.pod_name == "pod-resumed" and h.pod_ip == "10.1.0.9"
  assert reconnected._snapshots.created_manual_triggers == ["trig-1"]

  # When the batch watch has not yet closed an active handle whose resumed pod
  # already moved to a new IP, refresh() closes the stale connection, falls back
  # to get_sandbox when the watch still reports the old IP, and never leaks the
  # direct handle into _active_connection_sandboxes.
  c.sandbox_client._active_connection_sandboxes = {}
  h2 = f.acquire(t)
  active_old = h2.sandbox
  active_old.is_active = True
  active_old._pod_name = "stale-pod-2"
  active_old._seeded_pod_ip = "10.1.0.5"
  active_old.get_pod_name.return_value = "pod-resumed-2"
  active_old.get_pod_ip.return_value = "10.1.0.42"
  direct_sb = MagicMock(claim_name=h2.claim_name, sandbox_id=h2.sandbox_id, is_active=True)

  def fake_get_sandbox(claim_name, *, namespace):
    c.sandbox_client._active_connection_sandboxes[(namespace, claim_name)] = direct_sb
    return direct_sb

  c.sandbox_client.get_sandbox.side_effect = fake_get_sandbox

  h2.refresh()
  assert "_pod_name" not in vars(active_old)
  assert "_seeded_pod_ip" not in vars(active_old)
  active_old.close_connection.assert_called_once_with()
  c.sandbox_client.get_sandbox.assert_called_once_with(
      h2.claim_name, namespace=c.namespace)
  assert h2.sandbox is direct_sb
  assert c.sandbox_client._active_connection_sandboxes == {}
  c.sandbox_client._active_connection_sandboxes[(c.namespace, h2.claim_name)] = direct_sb
  f.release(h2)
  assert c.sandbox_client._active_connection_sandboxes == {}


def test_post_teardown_acquire_fails_before_creating_ondemand_pool(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.teardown()
  c.resources.ensure_template.reset_mock()
  c.resources.create_warmpool.reset_mock()
  with pytest.raises(FleetError):
    f.acquire(Task(id="late", image="unplanned:1"))
  c.resources.ensure_template.assert_not_called()
  c.resources.create_warmpool.assert_not_called()


def test_custom_fleet_labels_preserve_default_managed_label(make_cluster):
  from agent_sandbox_rl.testing import FakeFleet
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), labels={"team": "rl", "app": "custom"})
  t = f.load_tasks(["img"])[0]
  f.plan()
  f.acquire(t)
  batch_labels = c.batches[0].kwargs["labels"]
  assert batch_labels["team"] == "rl"
  for k, v in constants.DEFAULT_LABELS.items():
    assert batch_labels[k] == v
  fake = FakeFleet(FleetConfig(claim_mode="batch", labels={"team": "rl", "app": "custom"}))
  assert fake.config.labels["team"] == "rl"
  for k, v in constants.DEFAULT_LABELS.items():
    assert fake.config.labels[k] == v


def _recycle_on_a_fake_clock(monkeypatch):
  """Patches recycle and batch claims onto a clock that only each task advances, by
  100 s, and returns the batch settings under which a claim lives 399 s.

  After one task 299 s are left, more than twice the longest task plus the 5 s
  reset_timeout (205 s), so the claim is reset and reused. After two, 199 s are
  left, so it is rotated, and the third task runs on a second claim."""
  monkeypatch.setattr("agent_sandbox_rl.preflight.preflight_cluster",
                      lambda cluster, **kw: MagicMock(ok=True, checks=[]))
  monkeypatch.setattr(SandboxHandle, "exec", lambda self, cmd, **kw: _CLEAN)
  now = [0.0]
  clock = types.SimpleNamespace(monotonic=lambda: now[0])
  monkeypatch.setattr("agent_sandbox_rl.recycle.time", clock)
  monkeypatch.setattr("agent_sandbox_rl.batch_claims.time", clock)
  monkeypatch.setattr("agent_sandbox_rl.batch_claims.BATCH_SHUTDOWN_MARGIN_SECONDS", 0)

  def task(t, h):
    now[0] += 100
  return dict(claim_mode="batch", ready_timeout=100,
              batch_claims=BatchClaimConfig(work_budget=300)), task


def test_recycle_rotates_a_batch_claim_before_it_expires(make_cluster, monkeypatch):
  cfg, task = _recycle_on_a_fake_clock(monkeypatch)
  c = _batch_cluster(make_cluster)
  f = SandboxFleet(FleetConfig(**cfg), registry=ClusterRegistry([c]))
  f.load_tasks(["img"] * 3)
  f.plan()
  reuse_git_restore_sandbox(f, f.tasks, task, concurrency=1,
                            reset_timeout=5.0, use_session=False, scale_on_hold=False)
  assert len(c.batches[0].acquired) == 2
  f.teardown()


async def test_async_recycle_rotates_a_batch_claim_before_it_expires(make_cluster, monkeypatch):
  cfg, task = _recycle_on_a_fake_clock(monkeypatch)
  c = _batch_cluster(make_cluster)
  af = AsyncSandboxFleet(FleetConfig(**cfg), registry=ClusterRegistry([c]))
  af.load_tasks(["img"] * 3)
  af._fleet.plan()
  await reuse_git_restore_sandbox_async(af, af.tasks, task, concurrency=1,
                                        reset_timeout=5.0, use_session=False,
                                        scale_on_hold=False)
  assert len(c.batches[0].acquired) == 2
  await af.teardown()
  af.close()


def test_batch_claim_expires_at_accounts_for_subsecond_shutdown_truncation():
  bc = BatchClaims(BatchClaimConfig(work_budget=40), ready_timeout=0, labels={"run": "r1"})
  cluster = MagicMock(name="c1", namespace="default")
  fake_batch = MagicMock()
  fake_batch.err.return_value = None
  fake_batch.acquire.return_value = Member(
      claim_name="b1-1", sandbox_name="sb-1", warmpool="pool-a",
      pod_ips=("10.0.0.2",), state=MemberState.READY)
  cluster.sandbox_client.claim_batch.return_value = fake_batch

  t0 = time.monotonic()
  claim = bc.acquire(cluster, "pool-a")
  assert claim.expires_at - t0 <= 639.05


def test_batch_fail_fast_skips_pod_gets_on_warm_adoptions(make_cluster):
  c = _batch_cluster(make_cluster)
  open_batch = c.sandbox_client.claim_batch.side_effect

  def warm_batch(groups, **kwargs):
    b = open_batch(groups, **kwargs)
    b.warm_delay_s = 0.03
    return b
  c.sandbox_client.claim_batch.side_effect = warm_batch
  policy = FailFastPolicy(enabled=True, initial_delay_s=0.3, poll_s=0.01, grace_s=0)
  f = _fleet(ClusterRegistry([c]), fail_fast=policy)
  f.load_tasks(["img", "img"])
  f.plan()
  handles = [f.acquire(t) for t in f.tasks]
  time.sleep(0.05)
  assert len(handles) == 2
  assert c.core_api.read_namespaced_pod_status.call_count == 0
  assert c.custom_api.get_namespaced_custom_object.call_count == 0
  wd = f._batch_claims._watchdogs["solo"]
  assert wd.is_alive()
  f.teardown()
  wd.join(timeout=2)
  assert not wd.is_alive()


def test_batch_fail_fast_wakes_stuck_cold_start_with_sandbox_start_error(make_cluster):
  c = _batch_cluster(make_cluster)
  open_batch = c.sandbox_client.claim_batch.side_effect

  def cold_batch(groups, **kwargs):
    b = open_batch(groups, **kwargs)
    b.hold_pending = True
    return b
  c.sandbox_client.claim_batch.side_effect = cold_batch
  c.core_api.read_namespaced_pod_status.return_value = {
      "metadata": {"name": "sb-b-solo-0-1"},
      "status": {
          "phase": "Pending",
          "container_statuses": [{
              "name": "agent-runtime",
              "state": {"waiting": {"reason": "ImagePullBackOff",
                                    "message": "Back-off pulling image"}},
          }],
      },
  }
  policy = FailFastPolicy(enabled=True, initial_delay_s=0.02, poll_s=0.01, grace_s=0)
  f = _fleet(ClusterRegistry([c]), ready_timeout=30, fail_fast=policy)
  f.load_tasks(["img"])
  f.plan()
  observability._ensure_metrics()
  before_sf = (
      observability.CLAIMS.labels(cluster="solo", status="start_failed")._value.get()
      if observability.CLAIMS is not None else 0
  )
  before_err = (
      observability.CLAIMS.labels(cluster="solo", status="error")._value.get()
      if observability.CLAIMS is not None else 0
  )
  with pytest.raises(SandboxStartError) as ei:
    f.acquire(f.tasks[0])
  err = ei.value
  b = c.batches[0]
  assert err.reason == "ImagePullBackOff"
  assert err.claim_name == f"{b.batch_id}-1"
  assert err.pod_name == f"sb-{b.batch_id}-1"
  assert err.image == "img"
  assert isinstance(err.__cause__, BatchError)
  assert b.released_members == [f"{b.batch_id}-1"]
  if observability.CLAIMS is not None:
    after_sf = observability.CLAIMS.labels(
        cluster="solo", status="start_failed")._value.get()
    after_err = observability.CLAIMS.labels(
        cluster="solo", status="error")._value.get()
    assert after_sf == before_sf + 1
    assert after_err == before_err
  assert f.handles() == [] and c.active_claims == 0 and f._claims_reserved == 0
  wd = f._batch_claims._watchdogs["solo"]
  f.teardown()
  wd.join(timeout=2)
  assert not wd.is_alive()


def test_batch_fail_fast_stops_watchdog_on_replacement_and_respects_disabled(make_cluster):
  c = _batch_cluster(make_cluster)
  f_off = _fleet(ClusterRegistry([c]), fail_fast=FailFastPolicy(enabled=False))
  f_off.load_tasks(["img"])
  f_off.plan()
  f_off.acquire(f_off.tasks[0])
  assert f_off._batch_claims._watchdogs == {}
  f_off.teardown()

  c2 = _batch_cluster(make_cluster)
  f = _fleet(
      ClusterRegistry([c2]),
      fail_fast=FailFastPolicy(enabled=True, initial_delay_s=1.0, poll_s=0.05),
  )
  f.load_tasks(["img", "img"])
  f.plan()
  f.acquire(f.tasks[0])
  wd0 = f._batch_claims._watchdogs["solo"]
  assert wd0.is_alive()
  c2.batches[0].error = BatchError("lease lost")
  f.acquire(f.tasks[1])
  wd1 = f._batch_claims._watchdogs["solo"]
  assert wd1 is not wd0 and wd0._stop_evt.is_set()
  wd0.join(timeout=2)
  assert not wd0.is_alive() and wd1.is_alive()
  f.teardown()
  wd1.join(timeout=2)
  assert not wd1.is_alive()


def test_batch_watchdog_idle_poll_never_scans_members(make_cluster, monkeypatch):
  from agent_sandbox_rl.failfast import BatchWatchdog
  c = _batch_cluster(make_cluster)
  b = FakeBatch("solo", 0)
  for _ in range(50):
    b.acquire("pool-a")
  monkeypatch.setattr(b, "members", MagicMock(side_effect=AssertionError("members() called")))
  monkeypatch.setattr(b, "get_member", MagicMock(side_effect=AssertionError("get_member() called")))
  wd = BatchWatchdog(
      c, b, FailFastPolicy(enabled=True, initial_delay_s=1.0, poll_s=0.05),
      record_verdict=lambda cname, v: None)
  assert wd._poll_once() == 0.05


def test_batch_fail_fast_handles_ordinal_zero_and_continuous_load_after_unparsed_exit(
    make_cluster,
):
  from agent_sandbox_rl.batch_claims import extract_batch_claim_name

  assert extract_batch_claim_name(
      BatchError("acquired member 'b-0' was released")) == "b-0"
  assert extract_batch_claim_name(
      BatchError("batch 'b' stopped while acquiring 'b-1'")) == "b-1"
  assert extract_batch_claim_name(
      BatchError("no create in flight slot for claim 'b-2' before the deadline")) == "b-2"
  assert extract_batch_claim_name(
      BatchError("batch lost 'b-3' before connect: not ready")) == "b-3"

  c = _batch_cluster(make_cluster)
  open_batch = c.sandbox_client.claim_batch.side_effect

  def zero_indexed_cold_batch(groups, **kwargs):
    b = open_batch(groups, **kwargs)
    # Match SandboxBatch where BatchState._next_ordinal starts at 0 (<batch_id>-0).
    b._n = -1
    b.hold_pending = True
    return b

  c.sandbox_client.claim_batch.side_effect = zero_indexed_cold_batch
  c.core_api.read_namespaced_pod_status.side_effect = lambda name, *a, **kw: {
      "metadata": {"name": name},
      "status": {
          "phase": "Pending",
          "container_statuses": [{
              "name": "agent-runtime",
              "state": {"waiting": {"reason": "ImagePullBackOff",
                                    "message": "Back-off pulling image"}},
          }],
      },
  }
  policy = FailFastPolicy(enabled=True, initial_delay_s=0.01, poll_s=0.01, grace_s=0)
  f = _fleet(ClusterRegistry([c]), ready_timeout=1, fail_fast=policy, max_concurrent=4)
  f.load_tasks(["img", "img", "img"])
  f.plan()

  # Ordinal 0 (<batch_id>-0) fails fast rather than waiting out ready_timeout.
  with pytest.raises(SandboxStartError) as ei0:
    f.acquire(f.tasks[0])
  b = c.batches[0]
  assert ei0.value.claim_name == f"{b.batch_id}-0"
  assert ei0.value.pod_name == f"sb-{b.batch_id}-0"
  assert b.released_members == [f"{b.batch_id}-0"]

  # Keep one background acquire in flight (_in_flight_acquires >= 1) while
  # multiple acquires exit with unparsed errors and large ordinal jumps, then
  # verify a new cold-start claim still fails fast without stalling.
  bg_entered = threading.Event()
  bg_release = threading.Event()
  real_acquire = b.acquire

  def mixed_acquire(warmpool, timeout=None, *, pod_annotations=None):
    if pod_annotations == {"bg": "1"}:
      with b._lock:
        b._n += 1
        cname = f"{b.batch_id}-{b._n}"
        m = Member(claim_name=cname, sandbox_name=f"sb-{cname}",
                   warmpool=warmpool, pod_ips=("10.1.0.5",), state=MemberState.READY)
        b._members[cname] = m
      bg_entered.set()
      bg_release.wait(timeout=5.0)
      return m
    if pod_annotations == {"unparsed": "1"}:
      raise BatchError("transient acquire failure without claim name")
    return real_acquire(warmpool, timeout=timeout, pod_annotations=pod_annotations)

  b.acquire = mixed_acquire
  bg_err = []
  bg_handle = []

  def run_bg():
    try:
      bg_handle.append(f.acquire(f.tasks[1], pod_annotations={"bg": "1"}))
    except BaseException as exc:  # noqa: BLE001
      bg_err.append(exc)

  bg_thread = threading.Thread(target=run_bg)
  bg_thread.start()
  assert bg_entered.wait(timeout=2.0)

  for _ in range(5):
    with pytest.raises(BatchError, match="without claim name"):
      f.acquire(f.tasks[2], pod_annotations={"unparsed": "1"})

  with b._lock:
    b._n = 250
  with pytest.raises(SandboxStartError) as ei_late:
    f.acquire(f.tasks[2])
  assert ei_late.value.claim_name == f"{b.batch_id}-251"

  bg_release.set()
  bg_thread.join(timeout=2.0)
  assert bg_err == [] and len(bg_handle) == 1
  f.teardown()


def test_acquire_batch_and_release_all_run_concurrently_and_preserve_order(make_cluster):
  c = _batch_cluster(make_cluster)
  imgs = [f"img-{i}" for i in range(8)]
  f = _fleet(ClusterRegistry([c]), max_concurrent=8,
             fail_fast=FailFastPolicy(enabled=False))
  tasks = f.load_tasks(imgs)
  f.plan()
  # Open the batch once first so acquire_batch measures member acquires directly.
  h_warm = f.acquire(tasks[0])
  f.release(h_warm)
  b = c.batches[0]
  real_acquire = b.acquire
  real_release_member = b.release_member
  sleep_s = 0.10

  def slow_acquire(warmpool, timeout=None, *, pod_annotations=None):
    # Reverse stagger slightly so later tasks complete first while input order stays intact.
    time.sleep(sleep_s)
    return real_acquire(warmpool, timeout=timeout, pod_annotations=pod_annotations)

  def slow_release(member):
    time.sleep(sleep_s)
    return real_release_member(member)

  b.acquire = slow_acquire
  b.release_member = slow_release

  t0 = time.monotonic()
  handles = f.acquire_batch(tasks)
  acq_dt = time.monotonic() - t0
  assert acq_dt < 4 * sleep_s, f"acquire_batch took {acq_dt:.3f}s (expected < {4 * sleep_s:.3f}s)"
  assert [h.task.image for h in handles] == imgs

  t1 = time.monotonic()
  f.release_all()
  rel_dt = time.monotonic() - t1
  assert rel_dt < 4 * sleep_s, f"release_all took {rel_dt:.3f}s (expected < {4 * sleep_s:.3f}s)"
  assert f.handles() == [] and c.active_claims == 0 and f._claims_reserved == 0
  f.teardown()


def test_acquire_batch_rolls_back_succeeded_handles_on_partial_failure(make_cluster):
  c = _batch_cluster(make_cluster)
  imgs = [f"img-{i}" for i in range(6)]
  f = _fleet(ClusterRegistry([c]), max_concurrent=6,
             fail_fast=FailFastPolicy(enabled=False))
  tasks = f.load_tasks(imgs)
  f.plan()
  bad_pool = f.plan_.for_image("img-3").pool
  open_batch = c.sandbox_client.claim_batch.side_effect

  def batch_with_bad_pool(groups, **kwargs):
    b = open_batch(groups, **kwargs)
    real_acq = b.acquire

    def maybe_fail(warmpool, timeout=None, *, pod_annotations=None):
      if warmpool == bad_pool:
        time.sleep(0.03)
        raise RuntimeError("boom on img-3")
      return real_acq(warmpool, timeout=timeout, pod_annotations=pod_annotations)

    b.acquire = maybe_fail
    return b

  c.sandbox_client.claim_batch.side_effect = batch_with_bad_pool
  with pytest.raises(RuntimeError, match="boom on img-3"):
    f.acquire_batch(tasks)
  assert f.handles() == []
  assert c.active_claims == 0 and f._claims_reserved == 0
  assert len(c.batches[0].released_members) == len(c.batches[0].acquired)
  f.teardown()

  from agent_sandbox_rl.exceptions import FleetOvercommitError
  from agent_sandbox_rl.testing import FakeFleet
  fake = FakeFleet(FleetConfig(claim_mode="batch", max_live_sandboxes=2))
  fake_tasks = fake.load_tasks(["img-0", "img-1", "img-2"])
  with pytest.raises(FleetOvercommitError):
    fake.acquire_batch(fake_tasks)
  assert fake.handles() == [] and fake._claims_reserved == 0


def test_release_all_attempts_all_handles_before_reraising_first_error(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), max_concurrent=4,
             fail_fast=FailFastPolicy(enabled=False))
  tasks = f.load_tasks(["img"] * 4)
  f.plan()
  handles = f.acquire_batch(tasks)
  b = c.batches[0]
  bad_claim = handles[1].claim_name
  real_rel = b.release_member

  def fail_one(member):
    if member.claim_name == bad_claim:
      raise RuntimeError("release failed on 1")
    return real_rel(member)

  b.release_member = fail_one
  with pytest.raises(RuntimeError, match="release failed on 1"):
    f.release_all()
  # All 3 healthy handles were released; only the failed handle stays tracked.
  assert [h.claim_name for h in f.handles()] == [bad_claim]
  assert sorted(b.released_members) == sorted(
      h.claim_name for h in handles if h.claim_name != bad_claim
  )
  assert c.active_claims == 1 and f._claims_reserved == 1
  b.release_member = real_rel
  f.teardown()


def test_teardown_sweeps_remaining_handles_and_pools_when_handle_release_raises(make_cluster):
  c = _batch_cluster(make_cluster)
  c.resources.list_warmpools.return_value = ["pool-run-1"]
  c.resources.list_templates.return_value = ["tmpl-run-1"]
  f = _fleet(ClusterRegistry([c]), max_concurrent=4,
             fail_fast=FailFastPolicy(enabled=False))
  tasks = f.load_tasks(["img", "img"])
  f.plan()
  h0 = f.acquire(tasks[0])
  h1 = f.acquire(tasks[1])
  assert h0 in f.handles() and h1 in f.handles()
  h0.release = MagicMock(side_effect=RuntimeError("apiserver 500 on h0"))
  with pytest.raises(RuntimeError, match="apiserver 500 on h0"):
    f.teardown()
  assert h1.claim_name in c.batches[0].released_members
  c.resources.delete_warmpool.assert_called_once_with("pool-run-1")
  c.resources.delete_template.assert_called_once_with("tmpl-run-1")


def test_dynamic_ondemand_acquires_call_ensure_pool_once_under_concurrency(make_cluster):
  from agent_sandbox_rl import Task
  c = _batch_cluster(make_cluster)
  pool_ready = threading.Event()

  def slow_create_pool(*args, **kwargs):
    time.sleep(0.03)
    pool_ready.set()

  c.resources.create_warmpool.side_effect = slow_create_pool
  open_batch = c.sandbox_client.claim_batch.side_effect

  def checked_open_batch(groups, **kwargs):
    assert pool_ready.is_set(), "claim_batch ran before on-demand warmpool creation finished"
    return open_batch(groups, **kwargs)

  c.sandbox_client.claim_batch.side_effect = checked_open_batch
  f = _fleet(ClusterRegistry([c]), max_concurrent=16,
             fail_fast=FailFastPolicy(enabled=False))

  barrier = threading.Barrier(10)
  out = []
  errs = []

  def worker(i):
    try:
      barrier.wait(timeout=5)
      out.append(f.acquire(Task(id=f"t-{i}", image="unplanned:v1")))
    except BaseException as exc:  # noqa: BLE001
      errs.append(exc)

  threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
  for th in threads:
    th.start()
  for th in threads:
    th.join()
  assert errs == [] and len(out) == 10
  for j in range(5):
    out.append(f.acquire(Task(id=f"seq-{j}", image="unplanned:v1")))
  assert len(out) == 15
  assert c.resources.ensure_template.call_count == 1
  assert c.resources.create_warmpool.call_count == 1
  assert c.active_replicas == 1
  f.teardown()
