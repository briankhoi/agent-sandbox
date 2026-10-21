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

from agent_sandbox_rl import (BatchClaimConfig, ClusterConfig, ClusterRegistry,
                              FleetConfig, SandboxFleet, constants,
                              reuse_git_restore_sandbox)
from agent_sandbox_rl.cluster import Cluster
from agent_sandbox_rl.handles import SandboxHandle
from agent_sandbox_rl.recycle import _claim_expiring

_CLEAN = "PRISTINE=abc\nHEAD=abc\nDIRTY=0\nENV=e1\nCFG=g1\nPROCS=5"


class FakeBatch:
  """Stands in for SandboxBatch: acquire hands out Ready members at once."""

  def __init__(self, cluster, seq):
    self.batch_id = f"b-{cluster}-{seq}"
    self._cluster = cluster
    self._n = 0
    self._lock = threading.Lock()
    self.error = None
    self.acquired = []
    self.released_members = []
    self.released = False
    self.acquire_error = None

  def err(self):
    return self.error

  def acquire(self, warmpool, timeout=None):
    if self.acquire_error is not None:
      raise self.acquire_error
    with self._lock:
      self._n += 1
      n = self._n
    m = Member(claim_name=f"{self.batch_id}-{n}", sandbox_name=f"sb-{self.batch_id}-{n}",
               warmpool=warmpool, pod_ips=("10.1.0.5",), state=MemberState.READY)
    self.acquired.append((warmpool, timeout))
    return m

  def connect(self, member):
    s = MagicMock()
    s.claim_name = member.claim_name
    s.sandbox_id = member.sandbox_name
    return s

  def release_member(self, member):
    self.released_members.append(member.claim_name)

  def release(self):
    self.released = True


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
  c.batch_sandbox_client = lambda: c.sandbox_client
  return c


def _fleet(registry, **cfg):
  return SandboxFleet(FleetConfig(claim_mode="batch", **cfg), registry=registry)


def test_claim_mode_defaults_to_per_claim_and_rejects_unknown():
  assert FleetConfig().claim_mode == "per_claim"
  assert constants.CLAIM_MODES == ("per_claim", "batch")
  with pytest.raises(ValidationError, match="claim_mode"):
    FleetConfig(claim_mode="bulk")
  with pytest.raises(ValidationError):
    BatchClaimConfig(lease_duration=0)


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
  c.sandbox_client.create_sandbox.assert_not_called()

  h = handles[0]
  # Identity comes from the member, with no Sandbox or pod GETs.
  assert h.pod_name == h.sandbox_id == f"sb-{b.batch_id}-1"
  assert h.claim_name == f"{b.batch_id}-1"
  assert h.pod_ip == "10.1.0.5"
  assert h.sandbox.claim_name == h.claim_name     # batch.connect(member)
  assert h.claim_expires_at is not None
  assert c.active_claims == 3


def test_concurrent_first_acquires_open_one_batch(make_cluster):
  c = _batch_cluster(make_cluster)
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


def test_failed_acquire_frees_the_slot(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), max_live_sandboxes=1)
  f.load_tasks(["img", "img"])
  f.plan()
  f.acquire(f.tasks[0])                     # opens the batch
  c.batches[0].acquire_error = BatchError("not ready in time")
  f.release(f.handles()[0])
  with pytest.raises(BatchError):
    f.acquire(f.tasks[1])
  assert f._claims_reserved == 0 and c.active_claims == 0
  c.batches[0].acquire_error = None
  f.acquire(f.tasks[1])                     # the slot was given back


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


def test_pinned_acquire_keeps_the_per_claim_path(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  t = f.load_tasks(["img"])[0]
  f.plan()
  h = f.acquire(t, pod_annotations={"a": "b"})
  c.sandbox_client.create_sandbox.assert_called_once()
  assert c.batches == [] and h.claim_expires_at is None
  f.release(h)
  h.sandbox.terminate.assert_called_once()


def test_per_claim_mode_opens_no_batch(make_cluster):
  c = _batch_cluster(make_cluster)
  f = SandboxFleet(FleetConfig(), registry=ClusterRegistry([c]))
  t = f.load_tasks(["img"])[0]
  f.plan()
  h = f.acquire(t)
  assert c.batches == [] and h.claim_expires_at is None
  c.sandbox_client.create_sandbox.assert_called_once()


def test_claim_expiring():
  h = SandboxHandle(task=None, cluster_name="c", claim_name="x", sandbox_id="x",
                    pod_name="x", hostname="x")
  assert not _claim_expiring(h, 1e9, 1e9)   # no shutdownTime: never expires
  h._batch_claim = MagicMock(expires_at=time.monotonic() + 100)
  assert _claim_expiring(h, 45, 20)         # 100 s left < 2*45 + 20
  assert not _claim_expiring(h, 30, 20)     # 100 s left >= 2*30 + 20


def test_recycle_rotates_a_batch_claim_before_it_expires(make_cluster, monkeypatch):
  monkeypatch.setattr("agent_sandbox_rl.preflight.preflight_cluster",
                      lambda cluster, **kw: MagicMock(ok=True, checks=[]))
  monkeypatch.setattr(SandboxHandle, "exec", lambda self, cmd: _CLEAN)
  c = _batch_cluster(make_cluster)
  # With the 600 s margin patched out, a claim lives ready_timeout + work_budget
  # = 2 s, which is less than reset_timeout, so every claim is expiring.
  f = _fleet(ClusterRegistry([c]), ready_timeout=1,
             batch_claims=BatchClaimConfig(work_budget=1))
  f.load_tasks(["img"] * 3)
  f.plan()
  monkeypatch.setattr("agent_sandbox_rl.batch_claims.BATCH_SHUTDOWN_MARGIN_SECONDS", 0)
  reuse_git_restore_sandbox(f, f.tasks, lambda t, h: 1, concurrency=1,
                            reset_timeout=5.0, use_session=False, scale_on_hold=False)
  # Each claim serves one task and is rotated instead of reset.
  assert len(c.batches[0].acquired) == 3
  f.teardown()


def test_batch_sandbox_client_points_leases_at_the_cluster():
  configuration = client.Configuration()
  configuration.host = "https://cluster-b.example"
  api_client = client.ApiClient(configuration)
  c = Cluster(ClusterConfig(name="b"), api_client=api_client)
  c._k8s_helper = types.SimpleNamespace()
  c._sandbox_client = MagicMock()
  assert c.batch_sandbox_client() is c._sandbox_client
  lease_api = c._k8s_helper.coordination_v1_api
  assert lease_api.api_client.configuration.host == "https://cluster-b.example"
  assert lease_api.api_client.configuration.retries is False
  c.batch_sandbox_client()
  assert c._k8s_helper.coordination_v1_api is lease_api   # set once
