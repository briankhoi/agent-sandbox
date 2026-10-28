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
from kubernetes import client
from pydantic import ValidationError

from agent_sandbox_rl import (AsyncSandboxFleet, BatchClaimConfig,
                              ClusterRegistry, FleetConfig, SandboxFleet, constants,
                              reuse_git_restore_sandbox,
                              reuse_git_restore_sandbox_async)
from agent_sandbox_rl.exceptions import FleetError
from agent_sandbox_rl.handles import SandboxHandle

_CLEAN = "PRISTINE=abc\nHEAD=abc\nDIRTY=0\nENV=e1\nCFG=g1\nPROCS=5"


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

  def members(self, warmpool=None):
    with self._lock:
      return [m for m in self._members.values()
              if warmpool is None or m.warmpool == warmpool]

  def acquire(self, warmpool, timeout=None, *, pod_annotations=None):
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
    s = MagicMock()
    s.claim_name = member.claim_name
    s.sandbox_id = member.sandbox_name
    self.connected.append(s)
    return s

  def release_member(self, member):
    with self._lock:
      self._members.pop(member.claim_name, None)
    self.released_members.append(member.claim_name)

  def release(self):
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
  # One size-0 group, from the first acquire's pool only: claim_batch prechecks
  # every group serially, so the others are checked by acquire instead.
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
  h.sandbox.terminate.assert_not_called()   # would delete the claim behind the batch
  assert c.active_claims == 0 and f._claims_reserved == 0


def test_teardown_releases_every_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img", "img"])
  f.plan()
  f.acquire(f.tasks[0])
  c.batches[0].error = BatchError("lease lost")
  f.acquire(f.tasks[1])                     # the stopped batch is replaced
  assert len(c.batches) == 2
  f.teardown()
  assert all(b.released for b in c.batches)


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


def test_claim_landing_after_teardown_is_released_through_the_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img", "img"])
  f.plan()
  f.acquire(f.tasks[0])
  b = c.batches[0]
  claim_member = b.acquire

  def acquire_during_teardown(warmpool, timeout=None, *, pod_annotations=None):
    member = claim_member(warmpool, timeout, pod_annotations=pod_annotations)
    f.teardown()
    return member
  b.acquire = acquire_during_teardown
  with pytest.raises(FleetError):
    f.acquire(f.tasks[1])
  assert b.released_members[-1] == f"{b.batch_id}-2"
  assert not b.connected[-1].terminate.called


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
  f.release(old)
  assert c.batches[0].released_members == [old.claim_name]


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


def _recycle_on_a_fake_clock(monkeypatch):
  """Patches recycle and batch claims onto a clock that only each task advances, by
  100 s, and returns the batch settings under which a claim lives 400 s.

  After one task 300 s are left, more than twice the longest task plus the 5 s
  reset_timeout (205 s), so the claim is reset and reused. After two, 200 s are
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
