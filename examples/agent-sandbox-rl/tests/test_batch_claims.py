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
                              ClusterRegistry, FleetConfig, SandboxFleet, constants)
from agent_sandbox_rl.batch_claims import BatchClaims
from agent_sandbox_rl.exceptions import FleetError
from agent_sandbox_rl.sources import Task


class FakeBatch:
  """Stands in for SandboxBatch: acquire hands out Ready members at once."""

  def __init__(self, cluster, seq):
    self.batch_id = f"b-{cluster}-{seq}"
    self._cluster = cluster
    self._n = 0
    self._lock = threading.Lock()
    self._members: dict[str, Member] = {}
    self.error = None
    self.acquired = []
    self.acquired_annotations = []
    self.connected = []
    self.released_members = []
    self.released = False
    self.acquire_error = None

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
      m = Member(claim_name=f"{self.batch_id}-{n}", sandbox_name=f"sb-{self.batch_id}-{n}",
                 warmpool=warmpool, pod_ips=("10.1.0.5",), state=MemberState.READY)
      self._members[m.claim_name] = m
    self.acquired.append((warmpool, timeout))
    self.acquired_annotations.append(pod_annotations)
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

  def release(self):
    with self._lock:
      self.released = True


def _batch_cluster(make_cluster, name="solo"):
  c = make_cluster(name)
  c.batches = []
  c.exec_core_api = lambda: c.core_api

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
  assert c.batches == []
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
