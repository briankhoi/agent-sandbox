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

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from k8s_agent_sandbox import BatchError, Member, MemberState
from k8s_agent_sandbox.batch_utils import BATCH_REQUEST_TIMEOUT_SECONDS
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
    self.release_members_calls = 0
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

  def release_members(self):
    self._check_healthy()
    with self._lock:
      self.release_members_calls += 1
      self._members.clear()
      waiters = list(self._waiters.values())
      self._waiters.clear()
    for ev in waiters:
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
  c.resources.delete_claim.assert_called_once_with(
      h0.claim_name, request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS)
  # Healthy batches skip per-member DELETEs during teardown and delete via batch.release().
  assert c.batches[1].released_members == []
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
  # raises BatchError on the released batch and rollback deletes the claim
  # directly with a bounded request_timeout.
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
  assert b.released_members == [] and b.released
  c.resources.delete_claim.assert_called_once_with(
      f"{b.batch_id}-2", request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS)
  assert f._claims_reserved == 0

  # Window 2 runs teardown after batch.connect returns, so fleet.acquire sees
  # _torndown, deletes the claim directly once release_member rejects the
  # released batch, and raises FleetError without calling sandbox.terminate().
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
  assert b2.released_members == [] and b2.released
  c2.resources.delete_claim.assert_called_once_with(
      f"{b2.batch_id}-2", request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS)
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
  c.resources.delete_claim.assert_called_with(
      old.claim_name, request_timeout=BATCH_REQUEST_TIMEOUT_SECONDS)
  assert not old.sandbox.terminate.called
  assert c.active_claims == 1 and f._claims_reserved == 1
  f.teardown()
  assert all(b.released for b in c.batches)


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


def test_batch_release_cleans_up_snapshot_manual_triggers(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]))
  f.load_tasks(["img", "img", "img"])
  f.plan()

  h1 = f.acquire(f.tasks[0])
  snap_engine = MagicMock()
  h1.sandbox._snapshots = snap_engine

  # If _batch_claim.release() raises (e.g. fallback delete_claim fails on a
  # stopped batch), delete_manual_triggers() must NOT run so retry state stays,
  # while close_connection() still closes the local client transport.
  c.batches[0].error = BatchError("lease lost")
  c.resources.delete_claim.side_effect = client.ApiException(status=503)
  with pytest.raises(client.ApiException):
    f.release(h1)
  snap_engine.delete_manual_triggers.assert_not_called()
  assert h1.sandbox._snapshots is snap_engine
  assert h1.sandbox.close_connection.call_count == 1

  # Once release succeeds, manual triggers are deleted and _snapshots cleared.
  c.resources.delete_claim.side_effect = None
  f.release(h1)
  snap_engine.delete_manual_triggers.assert_called_once_with()
  assert h1.sandbox._snapshots is None
  assert h1.sandbox.close_connection.call_count == 2

  # Configured MagicMock(snapshots=...) does not fabricate _snapshots first.
  c.batches[0].error = None
  h_mock = f.acquire(f.tasks[1])
  mock_engine = MagicMock()
  h_mock.sandbox = MagicMock(
      claim_name=h_mock.claim_name, sandbox_id=h_mock.sandbox_id,
      snapshots=mock_engine)
  f.release(h_mock)
  mock_engine.delete_manual_triggers.assert_called_once_with()
  assert h_mock.sandbox.close_connection.call_count == 1

  # Also works via public .snapshots attribute and tolerates best-effort errors.
  h2 = f.acquire(f.tasks[2])
  pub_engine = MagicMock()
  pub_engine.delete_manual_triggers.side_effect = RuntimeError("apiserver blip")
  h2.sandbox = types.SimpleNamespace(
      claim_name=h2.claim_name, sandbox_id=h2.sandbox_id, snapshots=pub_engine)
  f.release(h2)
  pub_engine.delete_manual_triggers.assert_called_once_with()
  f.teardown()


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


def test_batch_fail_fast_never_releases_handed_out_or_newly_ready_members(make_cluster):
  from agent_sandbox_rl.batch_claims import extract_batch_claim_name
  from agent_sandbox_rl.failfast import BatchWatchdog

  assert extract_batch_claim_name(
      BatchError("batch 'b-1' stopped while acquiring 'b-1-7'")) == "b-1-7"

  c = _batch_cluster(make_cluster)
  c.core_api.read_namespaced_pod_status.return_value = {
      "metadata": {"name": "sb-b-solo-0-1"},
      "status": {
          "phase": "Pending",
          "container_statuses": [{
              "name": "agent-runtime",
              "state": {"waiting": {"reason": "CrashLoopBackOff",
                                    "message": "back-off"},
                        "terminated": None, "running": None},
              "last_state": {"terminated": {"reason": "OOMKilled"}},
          }],
      },
  }
  policy = FailFastPolicy(enabled=True, initial_delay_s=0.01, poll_s=0.01, grace_s=0)
  f = _fleet(ClusterRegistry([c]), fail_fast=policy)
  f.load_tasks(["img"])
  f.plan()

  # Once acquire() returns a handle, flipping its member state back to
  # PENDING (e.g. suspend()/resume() or mid-rollout container restart) must never
  # cause BatchWatchdog to inspect or release the caller's live sandbox.
  h = f.acquire(f.tasks[0])
  b = c.batches[0]
  pool = f.plan_.for_image("img").pool
  with b._lock:
    b._members[h.claim_name] = Member(
        claim_name=h.claim_name, sandbox_name=h.sandbox_id,
        warmpool=pool, pod_ips=(), state=MemberState.PENDING)
  time.sleep(0.06)
  assert b.released_members == []
  assert c.core_api.read_namespaced_pod_status.call_count == 0
  assert f._batch_claims._verdicts == {}
  f.teardown()

  # If a slow cold-start member transitions PENDING -> READY while
  # _check_member() is in flight, BatchWatchdog must not release it or leak a verdict.
  c2 = _batch_cluster(make_cluster)
  b2 = FakeBatch("solo", 0)
  claim_name = f"{b2.batch_id}-9"
  b2._members[claim_name] = Member(
      claim_name=claim_name, sandbox_name=f"sb-{claim_name}",
      warmpool="pool-x", pod_ips=(), state=MemberState.PENDING)
  b2._waiters[claim_name] = threading.Event()
  verdicts = {}

  def flip_ready_during_read(*args, **kwargs):
    with b2._lock:
      b2._members[claim_name] = Member(
          claim_name=claim_name, sandbox_name=f"sb-{claim_name}",
          warmpool="pool-x", pod_ips=("10.1.0.8",), state=MemberState.READY)
    return {
        "metadata": {"name": f"sb-{claim_name}"},
        "status": {
            "phase": "Pending",
            "container_statuses": [{
                "name": "agent-runtime",
                "state": {"waiting": {"reason": "ImagePullBackOff",
                                      "message": "pulling"}},
            }],
        },
    }

  c2.core_api.read_namespaced_pod_status.side_effect = flip_ready_during_read
  wd = BatchWatchdog(
      c2, b2, FailFastPolicy(enabled=True, initial_delay_s=0.0, poll_s=0.01, grace_s=0),
      record_verdict=lambda cname, v: verdicts.__setitem__(cname, v))
  wd._first_seen_at[claim_name] = time.monotonic() - 1.0
  wd._poll_once()
  assert c2.core_api.read_namespaced_pod_status.call_count == 1
  assert b2.released_members == []
  assert verdicts == {}

  # Setting _detached exits BatchWatchdog.run() cleanly.
  b2._detached = True
  wd.start()
  wd.join(timeout=2)
  assert not wd.is_alive()


def test_concurrent_ondemand_rollback_preserves_shared_pool(make_cluster):
  from agent_sandbox_rl import Task
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), max_concurrent=4,
             fail_fast=FailFastPolicy(enabled=False))

  creator_in_acquire = threading.Event()
  sibling_reused = threading.Event()
  call_num = 0
  call_lock = threading.Lock()
  open_batch = c.sandbox_client.claim_batch.side_effect

  def rigged_open_batch(groups, **kwargs):
    b = open_batch(groups, **kwargs)
    real_acquire = b.acquire

    def rigged_acquire(warmpool, timeout=None, *, pod_annotations=None):
      nonlocal call_num
      with call_lock:
        call_num += 1
        me = call_num
      if me == 1:
        creator_in_acquire.set()
        sibling_reused.wait(timeout=5)
        raise BatchError("creator claim failed")
      h_mem = real_acquire(warmpool, timeout=timeout, pod_annotations=pod_annotations)
      sibling_reused.set()
      return h_mem

    b.acquire = rigged_acquire
    return b

  c.sandbox_client.claim_batch.side_effect = rigged_open_batch

  t1_err = []
  t2_out = []

  def run_creator():
    try:
      f.acquire(Task(id="t-1", image="unplanned:shared"))
    except BaseException as exc:  # noqa: BLE001
      t1_err.append(exc)

  def run_sibling():
    creator_in_acquire.wait(timeout=5)
    t2_out.append(f.acquire(Task(id="t-2", image="unplanned:shared")))

  th1 = threading.Thread(target=run_creator)
  th2 = threading.Thread(target=run_sibling)
  th1.start()
  th2.start()
  th1.join(timeout=5)
  th2.join(timeout=5)

  assert len(t1_err) == 1 and isinstance(t1_err[0], BatchError)
  assert len(t2_out) == 1
  # Because th2 shared the on-demand pool before th1 rolled back, th1 must NOT
  # delete the shared SandboxWarmPool/SandboxTemplate out from under th2.
  c.resources.delete_warmpool.assert_not_called()
  c.resources.delete_template.assert_not_called()
  assert c.active_replicas == 1
  assert ("solo", "unplanned:shared") in f._ondemand
  f.teardown()


def test_sync_acquire_batch_rolls_back_on_keyboard_interrupt(make_cluster, monkeypatch):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), max_concurrent=4,
             fail_fast=FailFastPolicy(enabled=False))
  tasks = f.load_tasks(["img-0", "img-1", "img-2"])
  f.plan()

  def interrupt_as_completed(fs):
    # Let worker threads complete before raising KeyboardInterrupt in the waiter.
    for fut in fs:
      fut.result(timeout=5)
    raise KeyboardInterrupt("ctrl-c")

  monkeypatch.setattr("agent_sandbox_rl.fleet.as_completed", interrupt_as_completed)
  with pytest.raises(KeyboardInterrupt):
    f.acquire_batch(tasks)
  assert f.handles() == []
  assert c.active_claims == 0 and f._claims_reserved == 0
  f.teardown()


def test_keyboard_interrupt_rolls_back_acquire_and_release(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), max_live_sandboxes=1,
             fail_fast=FailFastPolicy(enabled=False))
  tasks = f.load_tasks(["img-0"])
  f.plan()
  open_batch = c.sandbox_client.claim_batch.side_effect
  fail_connect = True

  def maybe_interrupt_batch(groups, **kwargs):
    b = open_batch(groups, **kwargs)
    real_connect = b.connect

    def connect_with_interrupt(member):
      if fail_connect:
        raise KeyboardInterrupt("SIGTERM")
      return real_connect(member)

    b.connect = connect_with_interrupt
    return b

  c.sandbox_client.claim_batch.side_effect = maybe_interrupt_batch
  with pytest.raises(KeyboardInterrupt):
    f.acquire(tasks[0])
  assert f._claims_reserved == 0
  assert len(c.batches) == 1 and len(c.batches[0].released_members) == 1

  fail_connect = False
  h = f.acquire(tasks[0])
  real_release = h.release
  h.release = MagicMock(side_effect=KeyboardInterrupt("SIGINT"))
  with pytest.raises(KeyboardInterrupt):
    f.release(h)
  assert h in f.handles() and f._claims_reserved == 1
  h.release = real_release
  f.teardown()


def test_setup_and_warm_images_preopen_batches_with_size_zero_groups(
    make_cluster, monkeypatch
):
  monkeypatch.setattr("agent_sandbox_rl.preflight.preflight_cluster",
                      lambda cluster, **kw: MagicMock(ok=True, checks=[]))
  c1 = _batch_cluster(make_cluster, "c1")
  c2 = _batch_cluster(make_cluster, "c2")
  f = _fleet(ClusterRegistry([c1, c2]), placement="round-robin",
             fail_fast=FailFastPolicy(enabled=False))
  f.load_tasks(["img-a", "img-b", "img-c", "img-d"])
  f.setup()
  # Both clusters pre-opened 1 batch each during setup() before any acquire().
  assert len(c1.batches) == 1 and len(c2.batches) == 1
  assert all(g.size == 0 for g in c1.batches[0].groups + c2.batches[0].groups)
  planned_pools = {e.pool for e in f.plan_.entries}
  opened_pools = {g.warmpool for g in c1.batches[0].groups + c2.batches[0].groups}
  assert opened_pools == planned_pools
  handles = f.acquire_batch(f.tasks)
  assert len(c1.batches) == 1 and len(c2.batches) == 1
  assert len(handles) == 4
  f.teardown()

  # Windowed strategies (warm_images(..., wait=True)) also pre-open on window 0.
  c3 = _batch_cluster(make_cluster, "c3")
  fw = _fleet(ClusterRegistry([c3]), fail_fast=FailFastPolicy(enabled=False))
  fw.load_tasks(["img-w0", "img-w1", "img-w2", "img-w3"])
  fw.plan()
  assert len(c3.batches) == 0
  fw.warm_images(["img-w0", "img-w1"], wait=True)
  assert len(c3.batches) == 1
  assert [g.warmpool for g in c3.batches[0].groups] == [
      fw.plan_.for_image("img-w0").pool,
      fw.plan_.for_image("img-w1").pool,
  ]
  # Window 1 keeps the already-open batch without opening a second one.
  fw.warm_images(["img-w2", "img-w3"], wait=True)
  assert len(c3.batches) == 1
  fw.teardown()


@pytest.mark.asyncio
async def test_release_all_uses_batch_release_members_and_reuses_batch_across_bursts(
    make_cluster, monkeypatch
):
  monkeypatch.setattr("agent_sandbox_rl.preflight.preflight_cluster",
                      lambda cluster, **kw: MagicMock(ok=True, checks=[]))
  c = _batch_cluster(make_cluster)
  af = AsyncSandboxFleet(
      FleetConfig(claim_mode="batch", max_concurrent=8,
                  fail_fast=FailFastPolicy(enabled=False)),
      registry=ClusterRegistry([c]),
  )
  tasks = af.load_tasks([f"img-{i}" for i in range(6)])
  await af.setup()
  assert len(c.batches) == 1
  b = c.batches[0]

  # Burst 1 -> release_all uses 1 batch.release_members() call (0 per-member DELETEs)
  h_burst1 = await af.acquire_batch(tasks)
  assert len(h_burst1) == 6
  assert c.active_claims == 6 and af._fleet._claims_reserved == 6
  await af.release_all()
  assert b.release_members_calls == 1
  assert b.released_members == []
  assert not b.released
  assert af.handles() == [] and c.active_claims == 0 and af._fleet._claims_reserved == 0
  for h in h_burst1:
    assert h.sandbox.close_connection.called

  # Burst 2 reuses the exact same batch (same Lease & shared watch)
  h_burst2 = await af.acquire_batch(tasks)
  assert len(h_burst2) == 6 and len(c.batches) == 1
  # A transient error in release_members preserves handles and slots for retry
  real_rm = b.release_members
  b.release_members = MagicMock(side_effect=RuntimeError("transient 500"))
  with pytest.raises(RuntimeError, match="transient 500"):
    await af.release_all()
  assert len(af.handles()) == 6 and c.active_claims == 6 and af._fleet._claims_reserved == 6
  b.release_members = real_rm
  await af.release_all()
  assert b.release_members_calls == 2
  assert af.handles() == [] and c.active_claims == 0 and af._fleet._claims_reserved == 0

  await af.teardown()
  assert b.released
  af.close()


def test_batch_watchdog_skips_claim_and_sandbox_gets_for_cold_pods(make_cluster):
  from agent_sandbox_rl.failfast import BatchWatchdog

  c = _batch_cluster(make_cluster)
  b = FakeBatch("solo", 0)
  verdicts = {}
  wd = BatchWatchdog(
      c, b, FailFastPolicy(enabled=True, initial_delay_s=0.0, poll_s=0.01, grace_s=0),
      record_verdict=lambda cname, v: verdicts.__setitem__(cname, v),
  )

  # 1. Unbound member (sandbox_name=None): 0 GET SandboxClaim, 0 GET Pod/status.
  c_unbound = f"{b.batch_id}-0"
  b._members[c_unbound] = Member(
      claim_name=c_unbound, sandbox_name=None, warmpool="pool-a",
      pod_ips=(), state=MemberState.PENDING)
  b._waiters[c_unbound] = threading.Event()
  wd._first_seen_at[c_unbound] = time.monotonic() - 1.0
  wd._poll_once()
  assert c.custom_api.get_namespaced_custom_object.call_count == 0
  assert c.core_api.read_namespaced_pod_status.call_count == 0

  # 2. Cold-born Sandbox (pod name == sandbox_name): 1 GET Pod/status, 0 GET Sandbox.
  c_cold = f"{b.batch_id}-1"
  b._members.pop(c_unbound, None)
  b._waiters.pop(c_unbound, None)
  b._members[c_cold] = Member(
      claim_name=c_cold, sandbox_name="sb-cold-1", warmpool="pool-a",
      pod_ips=(), state=MemberState.PENDING)
  b._waiters[c_cold] = threading.Event()
  wd._first_seen_at[c_cold] = time.monotonic() - 1.0
  c.core_api.read_namespaced_pod_status.return_value = {
      "metadata": {"name": "sb-cold-1"},
      "status": {
          "phase": "Pending",
          "container_statuses": [{
              "name": "agent-runtime",
              "state": {"waiting": {"reason": "ImagePullBackOff",
                                    "message": "Back-off pulling image"}},
          }],
      },
  }
  wd._poll_once()
  assert c.custom_api.get_namespaced_custom_object.call_count == 0
  assert c.core_api.read_namespaced_pod_status.call_count == 1
  assert verdicts[c_cold].pod_name == "sb-cold-1"
  assert b.released_members == [c_cold]

  # 3. Adopted Sandbox with POD_NAME_ANNOTATION: falls back to GET Sandbox when sb_name 404s.
  c_adopted = f"{b.batch_id}-2"
  b._members[c_adopted] = Member(
      claim_name=c_adopted, sandbox_name="sb-adopted-2", warmpool="pool-a",
      pod_ips=(), state=MemberState.PENDING)
  b._waiters[c_adopted] = threading.Event()
  wd._first_seen_at[c_adopted] = time.monotonic() - 1.0

  def pod_by_name(name, *args, **kwargs):
    if name == "sb-adopted-2":
      raise client.ApiException(status=404)
    return {
        "metadata": {"name": name},
        "status": {
            "phase": "Pending",
            "container_statuses": [{
                "name": "agent-runtime",
                "state": {"waiting": {"reason": "InvalidImageName",
                                      "message": "bad tag"}},
            }],
        },
    }

  c.core_api.read_namespaced_pod_status.side_effect = pod_by_name
  c.custom_api.get_namespaced_custom_object.return_value = {
      "metadata": {"annotations": {constants.POD_NAME_ANNOTATION: "wp-pod-99"}},
  }
  wd._poll_once()
  assert c.custom_api.get_namespaced_custom_object.call_count == 1
  assert verdicts[c_adopted].pod_name == "wp-pod-99"
  assert c_adopted in b.released_members


def test_teardown_surfaces_failed_batch_release_and_cleans_shared_run_id_claims(
    make_cluster,
):
  c = _batch_cluster(make_cluster)
  f = SandboxFleet(
      FleetConfig(
          claim_mode="batch",
          run_id="shared-job-1",
          fail_fast=FailFastPolicy(enabled=False),
      ),
      registry=ClusterRegistry([c]),
  )
  tasks = f.load_tasks(["img-a", "img-b"])
  f.plan()
  handles = f.acquire_batch(tasks)
  b = c.batches[0]
  b.release = MagicMock(side_effect=RuntimeError("deletecollection 503"))
  with pytest.raises(RuntimeError, match="deletecollection 503"):
    f.teardown()
  # Under shared_run_id, failed batch.release() falls back to direct claim deletes.
  deleted_names = [call.args[0] for call in c.resources.delete_claim.call_args_list]
  assert sorted(deleted_names) == sorted(h.claim_name for h in handles)
  # And if one handle on a batch has a custom release override, release_all avoids
  # calling release_members() behind its back.
  c2 = _batch_cluster(make_cluster, "c2")
  f2 = _fleet(ClusterRegistry([c2]), max_concurrent=4,
              fail_fast=FailFastPolicy(enabled=False))
  t2 = f2.load_tasks(["img"] * 3)
  f2.plan()
  hs2 = f2.acquire_batch(t2)
  hs2[0].release = MagicMock(side_effect=RuntimeError("custom h0 fail"))
  with pytest.raises(RuntimeError, match="custom h0 fail"):
    f2.release_all()
  assert c2.batches[0].release_members_calls == 0
  assert [h.claim_name for h in f2.handles()] == [hs2[0].claim_name]
  hs2[0].release = SandboxHandle.release.__get__(hs2[0], SandboxHandle)
  f2.teardown()


class _FakeAsyncBatch:
  def __init__(self, batch_id: str, groups, **kwargs):
    self.batch_id = batch_id
    self.groups = list(groups)
    self.kwargs = kwargs
    self.namespace = kwargs.get("namespace", "default")
    self._seq = 0
    self._members: dict[str, Member] = {}
    self.acquired: list[tuple[str, float | None]] = []
    self.released_members: list[str] = []
    self.release_members_calls = 0
    self.released = False
    self.error: Exception | None = None
    self._acquire_waiters: dict[str, object] = {}
    self._lock = asyncio.Lock()

  def err(self) -> Exception | None:
    return self.error

  def get_member(self, claim_name: str) -> Member | None:
    return self._members.get(claim_name)

  async def acquire(self, warmpool: str, timeout: float | None = None, *,
                    pod_annotations: dict[str, str] | None = None) -> Member:
    await asyncio.sleep(0)
    self._seq += 1
    cname = f"{self.batch_id}-{self._seq}"
    m = Member(
        claim_name=cname,
        sandbox_name=f"sb-{cname}",
        warmpool=warmpool,
        pod_ips=("10.2.0.5",),
        state=MemberState.READY,
    )
    self._members[cname] = m
    self.acquired.append((warmpool, timeout))
    return m

  async def connect(self, member: Member):
    sb = MagicMock(
        claim_name=member.claim_name,
        sandbox_id=member.sandbox_name,
        is_active=True,
    )
    sb.close_connection = AsyncMock()
    return sb

  async def release_member(self, member: Member) -> None:
    if self.error is not None or self.released:
      raise BatchError("stopped")
    self._members.pop(member.claim_name, None)
    self.released_members.append(member.claim_name)

  async def release_members(self) -> None:
    if self.error is not None or self.released:
      raise BatchError("stopped")
    self._members.clear()
    self.release_members_calls += 1

  async def release(self) -> None:
    self.released = True
    self._members.clear()


async def test_native_async_batch_acquires_without_threads_and_bulk_releases_wave():
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  c = Cluster(
      ClusterConfig(name="native-c1", namespace="ns1"),
      api_client=MagicMock(),
      async_api_client=MagicMock(),
  )
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.ensure_template.return_value = "tmpl"
  c.resources.create_warmpool.return_value = True
  c.resources.wait_for_pool_ready.return_value = True
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0

  async_batches: list[_FakeAsyncBatch] = []
  fake_async_client = MagicMock()

  async def _claim_batch(groups, **kwargs):
    b = _FakeAsyncBatch(f"ab-{len(async_batches) + 1}", groups, **kwargs)
    async_batches.append(b)
    return b

  fake_async_client.claim_batch = AsyncMock(side_effect=_claim_batch)
  fake_async_client.close = AsyncMock()
  c._async_sandbox_client = fake_async_client
  c._async_loop = asyncio.get_running_loop()
  c._async_init_lock = asyncio.Lock()

  af = AsyncSandboxFleet(
      FleetConfig(
          claim_mode="batch",
          max_concurrent=8,
          fail_fast=FailFastPolicy(enabled=False),
      ),
      registry=ClusterRegistry([c]),
  )
  af.load_tasks(["img-a"] * 4 + ["img-b"] * 4)
  af._fleet.plan()
  await af._prepare_async_batch_clusters(af._fleet.plan_.entries)
  assert len(async_batches) == 1
  # Zero sync batches opened on native-async cluster.
  assert af._fleet._batch_claims.has_live_batch("native-c1") is False
  # Zero ThreadPoolExecutor instantiated during native async acquire + process + bulk release!
  assert af._executor is None

  seen_threads: set[int] = set()

  async def _async_proc(task, handle):
    seen_threads.add(threading.get_ident())
    await asyncio.sleep(0)
    return handle.pod_ip

  out = await af._process_parallel(af.tasks, _async_proc, concurrency=8)
  assert out == ["10.2.0.5"] * 8
  assert af._executor is None
  assert seen_threads == {threading.get_ident()}
  # Wave of 8 @ concurrency=8 deferred individual deletes into 1 bulk release_members()!
  assert async_batches[0].released_members == []
  assert async_batches[0].release_members_calls == 1
  assert af.handles() == []
  assert af._fleet._claims_reserved == 0
  assert c.active_claims == 0

  await af.teardown()
  assert async_batches[0].released is True
  await af.aclose()


def test_batch_claim_config_rl_defaults_and_explicit_none_override():
  cfg = BatchClaimConfig()
  assert cfg.create_rps == 500.0
  assert cfg.max_in_flight == 64
  explicit_sdk = BatchClaimConfig(create_rps=None, max_in_flight=None)
  assert explicit_sdk.create_rps is None
  assert explicit_sdk.max_in_flight is None


@pytest.mark.asyncio
async def test_async_batch_handle_refresh_and_cancellation_cleanup(make_cluster):
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  c = Cluster(
      ClusterConfig(name="refresh-c1", namespace="ns1"),
      api_client=MagicMock(),
      async_api_client=MagicMock(),
  )
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0

  sync_sb = MagicMock()
  sync_sb.get_pod_name.return_value = "pod-resumed-1"
  sync_sb.get_pod_ip.return_value = "10.9.0.2"
  sync_sb.is_active = True
  c._sandbox_client = MagicMock()
  c._sandbox_client.get_sandbox.return_value = sync_sb

  connect_gate = asyncio.Event()
  connect_entered = asyncio.Event()
  released_claims: list[str] = []

  class _CancelOrRefreshBatch:
    def __init__(self, batch_id: str, groups, **_kwargs):
      self.batch_id = batch_id
      self.groups = list(groups)
      self._seq = 0
      self.released = False

    def err(self):
      return None

    async def acquire(self, pool: str, *, timeout: float, pod_annotations=None):
      self._seq += 1
      return Member(
          claim_name=f"cl-{self._seq}",
          warmpool=pool,
          state=MemberState.READY,
          sandbox_name=f"sb-{self._seq}",
          pod_name=f"pod-{self._seq}",
          pod_ips=("10.9.0.1",),
      )

    def get_member(self, claim_name: str):
      return None

    async def connect(self, member: Member):
      if member.claim_name == "cl-2":
        connect_entered.set()
        await connect_gate.wait()
      sb = MagicMock(
          claim_name=member.claim_name,
          sandbox_id=member.sandbox_name,
          is_active=True,
      )
      sb.get_pod_name = AsyncMock(return_value="async-pod")
      sb.get_pod_ip = AsyncMock(return_value="10.9.0.1")
      sb.close_connection = AsyncMock()
      return sb

    async def release_member(self, member: Member):
      released_claims.append(member.claim_name)

    async def release_members(self):
      return None

    async def release(self):
      self.released = True

  fake_async_client = MagicMock()
  fake_async_client.claim_batch = AsyncMock(
      side_effect=lambda groups, **kw: _CancelOrRefreshBatch("cr-1", groups, **kw)
  )
  fake_async_client.close = AsyncMock()
  c._async_sandbox_client = fake_async_client
  c._async_loop = asyncio.get_running_loop()
  c._async_init_lock = asyncio.Lock()

  af = AsyncSandboxFleet(
      FleetConfig(claim_mode="batch", max_concurrent=4, max_live_sandboxes=1,
                  fail_fast=FailFastPolicy(enabled=False)),
      registry=ClusterRegistry([c]),
  )
  t_list = af.load_tasks(["img-a", "img-a"])
  await af.plan()
  af._fleet._warmed["img-a"] = 2
  await af._prepare_async_batch_clusters(af._fleet.plan_.entries)

  # 1. refresh() on an AsyncSandbox handle switches cleanly to direct sync sandbox without setting coroutine as pod_name.
  h1 = await af.acquire(t_list[0])
  h1.refresh()
  assert h1.pod_name == "pod-resumed-1"
  assert isinstance(h1.pod_name, str)
  await af.release(h1)
  assert af._fleet._claims_reserved == 0

  # 2. Cancelling acquire() mid-connect releases the claim in background AND frees _claims_reserved immediately.
  t_cancel = asyncio.create_task(af.acquire(t_list[1]))
  await asyncio.wait_for(connect_entered.wait(), 2.0)
  assert af._fleet._claims_reserved == 1
  t_cancel.cancel()
  with pytest.raises(asyncio.CancelledError):
    await t_cancel
  await asyncio.sleep(0.02)
  assert af._fleet._claims_reserved == 0
  assert released_claims == ["cl-1", "cl-2"]
  connect_gate.set()
  await af.teardown()
  await af.aclose()


@pytest.mark.asyncio
async def test_async_precheck_pools_uses_bulk_lists_and_windowed_fleet_prechecks_windows(make_cluster):
  from unittest.mock import patch
  from kubernetes_asyncio import client as async_k8s_client
  from k8s_agent_sandbox.async_k8s_helper import AsyncK8sHelper
  from k8s_agent_sandbox.async_sandbox_batch import _precheck_pools as async_precheck_pools

  helper = AsyncK8sHelper.__new__(AsyncK8sHelper)
  helper._initialized = True
  helper._init_lock = asyncio.Lock()
  api = async_k8s_client.CustomObjectsApi.__new__(async_k8s_client.CustomObjectsApi)
  helper.custom_objects_api = api

  list_calls: list[str] = []

  async def _fake_list(_self, *, plural, **_kwargs):
    list_calls.append(plural)
    if plural == "sandboxwarmpools":
      return {
          "metadata": {},
          "items": [
              {
                  "metadata": {"name": f"pool-{i}"},
                  "spec": {"sandboxTemplateRef": {"name": f"tpl-{i}"}},
                  "status": {"readyReplicas": i + 1},
              }
              for i in range(5)
          ],
      }
    return {
        "metadata": {},
        "items": [{"metadata": {"name": f"tpl-{i}"}} for i in range(5)],
    }

  with patch.object(async_k8s_client.CustomObjectsApi, "list_namespaced_custom_object", _fake_list):
    ready = await async_precheck_pools(
        helper,
        [f"pool-{i}" for i in range(5)],
        "ns-1",
        max_concurrency=8,
    )
  assert ready == {f"pool-{i}": i + 1 for i in range(5)}
  assert sorted(list_calls) == ["sandboxtemplates", "sandboxwarmpools"]

  # Also verify windowed AsyncSandboxFleet calls precheck_pools on subsequent windows.
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  c = Cluster(
      ClusterConfig(name="win-c1", namespace="ns1"),
      api_client=MagicMock(),
      async_api_client=MagicMock(),
  )
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0
  seeded_windows: list[dict[str, int]] = []
  prechecked_windows: list[list[str]] = []

  class _FakeWindowAsyncBatch:
    def __init__(self, batch_id: str, groups, **kwargs):
      self.batch_id = batch_id
      self.groups = list(groups)
      self.released = False

    def err(self):
      return None

    async def seed_checked_pools(self, ready_by_pool):
      seeded_windows.append(dict(ready_by_pool))

    async def precheck_pools(self, pools, timeout=None):
      prechecked_windows.append(list(pools))

    async def acquire(self, pool: str, *, timeout: float, pod_annotations=None):
      return Member(
          claim_name=f"cl-{pool}",
          warmpool=pool,
          state=MemberState.READY,
          sandbox_name=f"sb-{pool}",
          pod_name=f"pod-{pool}",
          pod_ips=("10.3.0.9",),
      )

    def get_member(self, claim_name: str):
      return None

    async def connect(self, member: Member):
      sb = MagicMock(
          claim_name=member.claim_name,
          sandbox_id=member.sandbox_name,
          is_active=True,
      )
      sb.close_connection = AsyncMock()
      return sb

    async def release_members(self):
      return None

    async def release_member(self, member):
      return None

    async def release(self):
      self.released = True

  fake_async_client = MagicMock()
  fake_async_client.claim_batch = AsyncMock(
      side_effect=lambda groups, **kw: _FakeWindowAsyncBatch("wb-1", groups, **kw)
  )
  fake_async_client.close = AsyncMock()
  c._async_sandbox_client = fake_async_client
  c._async_loop = asyncio.get_running_loop()
  c._async_init_lock = asyncio.Lock()

  af = AsyncSandboxFleet(
      FleetConfig(
          claim_mode="batch",
          max_concurrent=4,
          window_size=2,
          fail_fast=FailFastPolicy(enabled=False),
      ),
      registry=ClusterRegistry([c]),
  )
  af.load_tasks(["img-0", "img-1", "img-2", "img-3"])
  af._fleet.warm_images = MagicMock(
      side_effect=lambda imgs, **_kw: af._fleet._warmed.update({img: 1 for img in imgs})
  )
  af._fleet.unwarm_images = MagicMock()
  af._fleet.preflight = MagicMock()

  async def _noop_proc(_t, h):
    return h.pod_name

  out = await af.run(_noop_proc, strategy="sliding", concurrency=2)
  assert len(out) == 4 and all(isinstance(x, str) and x.startswith("sb-pool-") for x in out)
  # Window 0 opened claim_batch with 2 pools; Window 1 seeded its 2 warm_images-verified pools with 0 extra LISTs!
  assert len(seeded_windows) == 1
  assert len(seeded_windows[0]) == 2
  assert prechecked_windows == []
  await af.aclose()


@pytest.mark.asyncio
async def test_sync_release_all_on_async_batch_handles_and_defer_tail_isolation(
    make_cluster, monkeypatch
):
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  monkeypatch.setattr("agent_sandbox_rl.preflight.preflight_cluster",
                      lambda cluster, **kw: MagicMock(ok=True, checks=[]))
  c = Cluster(ClusterConfig(name="c-sync-rel", namespace="ns1", in_cluster=True),
              api_client=MagicMock(),
              async_api_client=MagicMock())
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0

  released_members: list[str] = []
  fail_bulk = False

  class _FakeAsyncBatch:
    def __init__(self, batch_id: str, groups, **kwargs):
      self.batch_id = batch_id
      self.groups = list(groups)
      self._state = MagicMock()
      self._state.delete_seq.return_value = 7
      self._connected = {}
      self._ord = 0

    def err(self):
      return None

    async def acquire(self, pool: str, *, timeout: float, pod_annotations=None):
      self._ord += 1
      return Member(
          claim_name=f"cl-{self._ord}",
          warmpool=pool,
          state=MemberState.READY,
          sandbox_name=f"sb-{self._ord}",
          pod_name=f"pod-{self._ord}",
          pod_ips=("10.4.0.2",),
      )

    def get_member(self, claim_name: str):
      return None

    async def connect(self, member: Member):
      sb = MagicMock(
          claim_name=member.claim_name,
          sandbox_id=member.sandbox_name,
          is_active=True,
      )
      sb.close_connection = AsyncMock()
      return sb

    async def release_members(self):
      if fail_bulk:
        raise RuntimeError("simulated bulk deletecollection error")

    async def release_member(self, member):
      released_members.append(member.claim_name)

    async def release(self):
      return None

  fake_async_client = MagicMock()
  fake_async_client.claim_batch = AsyncMock(
      side_effect=lambda groups, **kw: _FakeAsyncBatch("ab-1", groups, **kw)
  )
  fake_async_client.close = AsyncMock()
  c._async_sandbox_client = fake_async_client
  c._async_loop = asyncio.get_running_loop()
  c._async_init_lock = asyncio.Lock()

  af = AsyncSandboxFleet(
      FleetConfig(claim_mode="batch", max_concurrent=4,
                  fail_fast=FailFastPolicy(enabled=False)),
      registry=ClusterRegistry([c]),
  )
  tasks = af.load_tasks(["img-a", "img-b", "img-c"])
  af._fleet.plan()
  for e in af._fleet.plan_.entries:
    af._fleet._warmed[e.image] = 1
  await af._prepare_async_batch_clusters(af._fleet.plan_.entries, just_warmed=True)

  # Finding #1 & #9.1: Sync SandboxFleet.release_all() on >=2 AsyncBatchClaim handles
  # must NOT leave an unawaited coroutine and must delete claims cleanly.
  h1 = await af.acquire(tasks[0])
  h2 = await af.acquire(tasks[1])
  await asyncio.to_thread(af._fleet.release_all)
  assert set(released_members) == {h1.claim_name, h2.claim_name}
  assert af.handles() == []

  # Finding #7: Foreign handle held outside _process_parallel is NOT bulk-deleted,
  # and bulk release errors are captured into results rather than crashing.
  foreign = await af.acquire(tasks[2])
  res_ok = await af._process_parallel(tasks[:2], lambda _t, h: h.pod_name, 2)
  assert len(res_ok) == 2 and all(isinstance(r, str) for r in res_ok)
  assert [h.claim_name for h in af.handles()] == [foreign.claim_name]
  await af.release(foreign)

  fail_bulk = True

  async def _yield_proc(_t, h):
    await asyncio.sleep(0.01)
    return h.pod_name

  res_err = await af._process_parallel(tasks[:2], _yield_proc, 2)
  assert len(res_err) == 2 and all(isinstance(r, RuntimeError) for r in res_err)
  fail_bulk = False
  await af.release_all()
  await af.aclose()


def test_pool_maxsize_resizes_aiohttp_connector_and_clamps_small_pools(make_cluster):
  from types import SimpleNamespace
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import BatchClaimConfig, ClusterConfig
  from agent_sandbox_rl.batch_claims import BatchClaims

  fake_connector = SimpleNamespace(_limit=100)
  fake_pm = SimpleNamespace(connector=fake_connector)
  fake_async_api = SimpleNamespace(
      configuration=SimpleNamespace(connection_pool_maxsize=100),
      rest_client=SimpleNamespace(pool_manager=fake_pm),
  )
  c = Cluster(
      ClusterConfig(name="c-conn", namespace="ns1", in_cluster=True),
      api_client=MagicMock(),
      async_api_client=fake_async_api,
      pool_maxsize=100,
  )
  c.ensure_pool_maxsize(600)
  assert fake_connector._limit == 600
  assert fake_async_api.configuration.connection_pool_maxsize == 600

  # Default BatchClaimConfig() clamps max_in_flight to pool_size - 2 on small injected pools.
  c_small = _batch_cluster(make_cluster, "c-small")
  c_small.sandbox_client.k8s_helper.api_pool_size = MagicMock(return_value=30)
  bc = BatchClaims(BatchClaimConfig(), ready_timeout=60, labels={})
  captured_mif: list[int | None] = []
  orig_cb = c_small.sandbox_client.claim_batch

  def spy_cb(groups, **kw):
    captured_mif.append(kw.get("max_in_flight"))
    return orig_cb(groups, **kw)

  c_small.sandbox_client.claim_batch = spy_cb
  bc.prepare_clusters([(c_small, ["pool-1"])])
  assert captured_mif == [28]
  bc.release_all()


@pytest.mark.asyncio
async def test_ondemand_adopt_precedence_ws_utf8_and_inline_lost_pod(make_cluster):
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig
  from agent_sandbox_rl.exceptions import (
      FleetError,
      FleetOvercommitError,
      PoolNotFoundError,
      SandboxLostError,
  )

  c = Cluster(
      ClusterConfig(name="c-od", namespace="ns1", in_cluster=True),
      api_client=MagicMock(),
      async_api_client=MagicMock(),
  )
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.ensure_template.return_value = "tmpl-od"
  c.resources.create_warmpool.return_value = "pool-od"
  c.resources.get_warmpool.return_value = {"metadata": {"uid": "u1", "labels": {}}}
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0

  af_adopt = AsyncSandboxFleet(
      FleetConfig(
          claim_mode="batch",
          adopt_existing=True,
          max_live_sandboxes=1,
          fail_fast=FailFastPolicy(enabled=False),
      ),
      registry=ClusterRegistry([c]),
  )
  af_adopt._fleet.plan_ = MagicMock(for_image=MagicMock(return_value=None))
  af_adopt._fleet._claims_reserved = 1
  with pytest.raises(FleetOvercommitError):
    await af_adopt.acquire(Task(id="t1", image="unplanned:v1"))
  af_adopt._fleet._claims_reserved = 0
  with pytest.raises(PoolNotFoundError):
    await af_adopt.acquire(Task(id="t1", image="unplanned:v1"))
  af_adopt._fleet._torndown = True
  with pytest.raises(FleetError):
    await af_adopt.acquire(Task(id="t1", image="unplanned:v1"))

  # Inline still_reaches() in aexec avoids read_namespaced_pod_status GET.
  dead_bc = MagicMock(expires_at=time.monotonic() + 60.0)
  dead_bc.still_reaches.return_value = False
  c.core_api = MagicMock()
  c._async_exec_core_api = MagicMock()
  h = SandboxHandle(
      task=Task(id="t-lost", image="img:1"),
      cluster_name="c-od",
      claim_name="cl-lost",
      sandbox_id="sb-lost",
      pod_name="pod-lost",
      hostname="sb-lost",
      pod_ip="10.0.0.9",
      sandbox=MagicMock(),
      _cluster=c,
      _batch_claim=dead_bc,
  )
  with pytest.MonkeyPatch.context() as mp:
    mp.setattr(
        "agent_sandbox_rl.handles.aexec_in_pod",
        AsyncMock(side_effect=RuntimeError("ws dropped")),
    )
    with pytest.raises(SandboxLostError):
      await h.aexec("true")
  c.core_api.read_namespaced_pod_status.assert_not_called()
  await af_adopt.aclose()


def test_sequential_event_loops_teardown_fencing_and_ws_multibyte_utf8():
  from types import SimpleNamespace
  from kubernetes_asyncio import client as async_k8s_client
  from kubernetes_asyncio.stream.ws_client import STDOUT_CHANNEL
  from agent_sandbox_rl.cluster import Cluster, _build_fast_ws_core_api
  from agent_sandbox_rl.config import ClusterConfig

  # 1) _FastWsApiClient preserves split multibyte UTF-8 across WebSocket frames ("🚀" = f0 9f 9a 80).
  async def _check_ws():
    base_cfg = async_k8s_client.Configuration()
    base_cfg.host = "https://127.0.0.1:6443"
    ws_api, _core = await _build_fast_ws_core_api(
        ClusterConfig(name="c-ws", namespace="ns1", in_cluster=True),
        pool_maxsize=32,
        base_configuration=base_cfg,
    )
    try:
      class _FakeWsCtx:
        async def __aenter__(self):
          async def _gen():
            yield SimpleNamespace(data=bytes([STDOUT_CHANNEL]) + b"\xf0\x9f")
            yield SimpleNamespace(data=bytes([STDOUT_CHANNEL]) + b"\x9a\x80")
          return _gen()
        async def __aexit__(self, *exc):
          return False

      ws_api.rest_client.pool_manager.ws_connect = lambda *a, **kw: _FakeWsCtx()
      resp = await ws_api.request("GET", "https://127.0.0.1:6443/api/v1/namespaces/ns1/pods/p/exec")
      assert resp.data.decode("utf-8") == "🚀"
    finally:
      await ws_api.close()

  asyncio.run(_check_ws())

  # 2) Sequential asyncio.run() calls on the same Cluster & AsyncBatchClaims reset stale loop state.
  base_cfg = async_k8s_client.Configuration()
  base_cfg.host = "https://127.0.0.1:6443"
  injected_async = MagicMock(configuration=base_cfg)
  c = Cluster(
      ClusterConfig(name="c-seq", namespace="ns1", in_cluster=True),
      api_client=MagicMock(),
      async_api_client=injected_async,
  )
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0

  opened_loops: list[asyncio.AbstractEventLoop] = []

  class _LoopBatch:
    def __init__(self, idx: int):
      self.batch_id = f"lb-{idx}"
      opened_loops.append(asyncio.get_running_loop())

    def err(self):
      return None

    async def release(self):
      return None

  from agent_sandbox_rl.batch_claims import AsyncBatchClaims
  abc = AsyncBatchClaims(BatchClaimConfig(), ready_timeout=60, labels={})

  async def _open_on_loop(idx: int):
    _, core = await _build_fast_ws_core_api(c.config, pool_maxsize=32, base_configuration=base_cfg)
    assert core is not None
    await c.async_exec_core_api()
    fake_sc = MagicMock()
    fake_sc.k8s_helper = MagicMock(api_pool_size=AsyncMock(return_value=64))
    fake_sc.claim_batch = AsyncMock(return_value=_LoopBatch(idx))
    fake_sc.close = AsyncMock()
    c._async_sandbox_client = fake_sc
    b = await abc._batch_for(c, "pool-1")
    assert b.batch_id == f"lb-{idx}"
    await c._async_ws_api_client.close()
    c._async_ws_api_client = None
    c._async_exec_core_api = None

  asyncio.run(_open_on_loop(1))
  asyncio.run(_open_on_loop(2))
  assert len(opened_loops) == 2 and opened_loops[0] is not opened_loops[1]
  # Cross-loop release_all() cleanly releases batches from closed Loop 1 as well as active Loop 3.
  sync_released: list[str] = []
  abc._sync_release_single_batch = lambda _cl, b, _bl, _lt: sync_released.append(b.batch_id)  # type: ignore[method-assign]
  assert asyncio.run(abc.release_all()) == {}
  assert sorted(sync_released) == ["lb-1", "lb-2"]


def test_pass2_ws_errors_connected_tuple_atexit_and_bound_executor():
  import aiohttp
  from types import SimpleNamespace
  from kubernetes_asyncio import client as async_k8s_client
  from kubernetes_asyncio.stream.ws_client import STDOUT_CHANNEL
  from agent_sandbox_rl.batch_claims import close_async_sandbox_sync
  from agent_sandbox_rl.cluster import Cluster, _build_fast_ws_core_api
  from agent_sandbox_rl.config import ClusterConfig

  async def _check_ws_edge_cases():
    base_cfg = async_k8s_client.Configuration()
    base_cfg.host = "https://127.0.0.1:6443"
    ws_api, core = await _build_fast_ws_core_api(
        ClusterConfig(name="c-ws2", namespace="ns1", in_cluster=True),
        pool_maxsize=16,
        base_configuration=base_cfg,
    )
    try:
      # 2a) Invalid UTF-8 bytes are replaced cleanly without UnicodeDecodeError in ApiClient.__call_api
      class _BinaryWsCtx:
        async def __aenter__(self):
          async def _gen():
            yield SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=bytes([STDOUT_CHANNEL]) + b"ok\xff\xfe!")
          return _gen()
        async def __aexit__(self, *exc):
          return False

      ws_api.rest_client.pool_manager.ws_connect = lambda *a, **kw: _BinaryWsCtx()
      out = await core.connect_get_namespaced_pod_exec("p", "ns1", command=["true"])
      assert "ok" in out and "\ufffd" in out

      # 2b) WSMsgType.ERROR mid-stream raises instead of silently truncating
      class _ErrWsCtx:
        async def __aenter__(self):
          class _Stream:
            def exception(self):
              return RuntimeError("pod killed mid-stream")
            def __aiter__(self):
              async def _gen():
                yield SimpleNamespace(type=aiohttp.WSMsgType.ERROR, data=RuntimeError("pod killed"))
              return _gen()
          return _Stream()
        async def __aexit__(self, *exc):
          return False

      ws_api.rest_client.pool_manager.ws_connect = lambda *a, **kw: _ErrWsCtx()
      with pytest.raises(RuntimeError, match="pod killed"):
        await core.connect_get_namespaced_pod_exec("p", "ns1", command=["true"])

      # 2c) WSServerHandshakeError(404) raises ApiException(status=404) (no AttributeError on None.decode)
      class _Handshake404Ctx:
        async def __aenter__(self):
          req_info = MagicMock()
          raise aiohttp.WSServerHandshakeError(req_info, (), status=404, message="Not Found")
        async def __aexit__(self, *exc):
          return False

      ws_api.rest_client.pool_manager.ws_connect = lambda *a, **kw: _Handshake404Ctx()
      with pytest.raises(async_k8s_client.ApiException) as exc_info:
        await core.connect_get_namespaced_pod_exec("p", "ns1", command=["true"])
      assert exc_info.value.status == 404
    finally:
      await ws_api.close()

  asyncio.run(_check_ws_edge_cases())

  # 3) close_async_sandbox_sync unpacks (sandbox, pod_ip) tuples and invokes _close_for_atexit on closed loops
  atexit_called: list[bool] = []

  class _FakeClosedLoopAsyncSb:
    def _close_for_atexit(self):
      atexit_called.append(True)

    async def close_connection(self):
      raise AssertionError("should use _close_for_atexit when loop is None/closed")

  close_async_sandbox_sync((_FakeClosedLoopAsyncSb(), "10.0.0.5"), None, None)
  assert atexit_called == [True]

  # 4) _is_default_executor recognizes bound self._process_parallel instances
  c_dummy = Cluster(ClusterConfig(name="c-d", namespace="ns1", in_cluster=True), api_client=MagicMock())
  af = AsyncSandboxFleet(FleetConfig(claim_mode="batch"), registry=ClusterRegistry([c_dummy]))
  bound_one = af._process_parallel
  assert af._is_default_executor(None) is True
  assert af._is_default_executor(bound_one) is True
  assert af._is_default_executor(lambda *a, **k: []) is False
  af.close()


def test_pass3_stale_dict_cross_loop_session_close_and_ondemand_deadlock_free(make_cluster):
  from types import SimpleNamespace
  import warnings
  from kubernetes_asyncio import client as async_k8s_client
  from agent_sandbox_rl.batch_claims import AsyncBatchClaims
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import BatchClaimConfig, ClusterConfig
  from agent_sandbox_rl.exceptions import FleetError

  # 1) _sync_release_single_batch closes dict-typed _stale_connections and clears before unregister
  c = _batch_cluster(make_cluster, "c-stale")
  stale_closed: list[str] = []

  class _StaleSb:
    def __init__(self, tag: str) -> None:
      self.tag = tag

    def _close_for_atexit(self) -> None:
      stale_closed.append(self.tag)

  seen_stale_on_unreg: list[int] = []
  s1, s2 = _StaleSb("s1"), _StaleSb("s2")
  fake_b = SimpleNamespace(
      namespace="ns1",
      batch_id="b-stale",
      _connected={"cl-0": (_StaleSb("c0"), "10.0.0.2")},
      _stale_connections={s1: None, s2: None},
      release=MagicMock(),
      _client=SimpleNamespace(
          _unregister_batch=lambda b: seen_stale_on_unreg.append(len(b._stale_connections))
      ),
  )
  abc = AsyncBatchClaims(BatchClaimConfig(), ready_timeout=30, labels={})
  abc._sync_release_single_batch(c, fake_b, None, None)
  assert set(stale_closed) == {"c0", "s1", "s2"}
  assert fake_b._stale_connections == {}
  assert fake_b.release.called

  # 2a) Sequential asyncio.run() calls on one Cluster leak 0 aiohttp ClientSessions
  def _make_local_async_api(_cfg, *, pool_maxsize=1000):
    acfg = async_k8s_client.Configuration()
    acfg.host = "https://127.0.0.1:6443"
    api = async_k8s_client.ApiClient(configuration=acfg)
    _ = api.rest_client.pool_manager
    return api

  async def _fake_build_async(cfg, *, pool_maxsize=1000):
    return _make_local_async_api(cfg, pool_maxsize=pool_maxsize)

  c_seq = Cluster(ClusterConfig(name="c-seq", namespace="ns1", in_cluster=True), api_client=MagicMock())
  c_seq._injected_sync_api_client = False
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    with pytest.MonkeyPatch.context() as mp:
      mp.setattr("agent_sandbox_rl.cluster.build_async_api_client", _fake_build_async)

      async def _loop1_init():
        await c_seq.get_async_sandbox_client()
        await c_seq.async_exec_core_api()

      asyncio.run(_loop1_init())
      api_r1 = c_seq._async_api_client
      ws_r1 = c_seq._async_ws_api_client
      assert not api_r1.rest_client.pool_manager.closed
      assert not ws_r1.rest_client.pool_manager.closed
      # Second asyncio.run() transitions away from closed loop 1 -> closes loop 1 sessions cleanly!
      asyncio.run(c_seq.get_async_sandbox_client())
      assert api_r1.rest_client.pool_manager.closed
      assert ws_r1.rest_client.pool_manager.closed
      asyncio.run(c_seq.aclose())
  unclosed_warnings = [w for w in caught if "Unclosed client session" in str(w.message)]
  assert unclosed_warnings == []

  # 2b) Running background loop whose session was closed falls back to sync release in release_all()
  bg_loop = asyncio.new_event_loop()
  bg_ready = threading.Event()
  bg_thread = threading.Thread(
      target=lambda: (asyncio.set_event_loop(bg_loop), bg_ready.set(), bg_loop.run_forever()),
      daemon=True,
  )
  bg_thread.start()
  bg_ready.wait(2.0)
  try:
    sync_rel_calls: list[str] = []

    async def _fail_if_async_called():
      raise RuntimeError("Session is closed")

    closed_pm = SimpleNamespace(closed=True)
    closed_api = SimpleNamespace(rest_client=SimpleNamespace(pool_manager=closed_pm))
    closed_helper = SimpleNamespace(_api_client=closed_api)
    closed_batch = SimpleNamespace(
        batch_id="b-bg-closed",
        namespace="ns1",
        _client=SimpleNamespace(k8s_helper=closed_helper),
        _connected={},
        _stale_connections={},
        err=lambda: None,
        release=_fail_if_async_called,
    )
    abc_bg = AsyncBatchClaims(BatchClaimConfig(), ready_timeout=30, labels={})
    abc_bg._opened.append((c, closed_batch, bg_loop, bg_thread))
    abc_bg._sync_release_single_batch = lambda cl, b, lp, lt: sync_rel_calls.append(b.batch_id)  # type: ignore[method-assign]
    failed = asyncio.run(abc_bg.release_all())
    assert failed == {}
    assert sync_rel_calls == ["b-bg-closed"]
  finally:
    bg_loop.call_soon_threadsafe(bg_loop.stop)
    bg_thread.join(timeout=2.0)
    bg_loop.close()

  # 3) _ensure_ondemand_pool_for_acquire rollback on mid-create teardown race does not deadlock
  c_od = _batch_cluster(make_cluster, "c-od")
  f_od = _fleet(ClusterRegistry([c_od]), fail_fast=FailFastPolicy(enabled=False))

  def _ensure_then_teardown(cl, img, reps):
    f_od._torndown = True
    return f_od.config.pool_name(img)

  f_od._ensure_pool = _ensure_then_teardown  # type: ignore[method-assign]
  done_evt = threading.Event()
  err_box: list[BaseException] = []

  def _run_od():
    try:
      f_od._ensure_ondemand_pool_for_acquire(c_od, "img-race:1")
    except BaseException as exc:  # noqa: BLE001
      err_box.append(exc)
    finally:
      done_evt.set()

  th = threading.Thread(target=_run_od, daemon=True)
  th.start()
  assert done_evt.wait(2.0), "_ensure_ondemand_pool_for_acquire deadlocked on _ondemand_lock!"
  assert len(err_box) == 1 and isinstance(err_box[0], FleetError)
  assert c_od.active_replicas == 0


def test_pass4_offline_socket_cleanup_same_loop_rebuild_and_midflight_closed_session(
    make_cluster,
):
  import gc
  import socket
  from types import SimpleNamespace
  import warnings
  from kubernetes_asyncio import client as async_k8s_client
  from k8s_agent_sandbox.async_k8s_helper import AsyncK8sHelper
  from agent_sandbox_rl.batch_claims import (
      AsyncBatchClaim,
      AsyncBatchClaims,
      _helper_async_session_open,
  )
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import BatchClaimConfig, ClusterConfig
  from agent_sandbox_rl.failfast import BatchWatchdog

  # 1) Real local HTTP/1.1 keep-alive server: sequential asyncio.run() AND stopped-loop aclose()
  #    with live pooled sockets in connector._conns leak 0 socket FDs and raise 0 cross-loop errors.
  srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
  srv.bind(("127.0.0.1", 0))
  srv.listen(16)
  port = srv.getsockname()[1]
  stop_srv = threading.Event()
  accepted: list[socket.socket] = []

  def _serve() -> None:
    srv.settimeout(0.05)
    while not stop_srv.is_set():
      try:
        conn, _ = srv.accept()
      except TimeoutError:
        continue
      except OSError:
        break
      accepted.append(conn)
      conn.settimeout(0.2)
      try:
        while not stop_srv.is_set():
          data = conn.recv(4096)
          if not data:
            break
          conn.sendall(
              b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: keep-alive\r\n\r\nok"
          )
      except OSError:
        pass
      finally:
        try:
          conn.close()
        except OSError:
          pass

  srv_th = threading.Thread(target=_serve, daemon=True)
  srv_th.start()
  try:
    async def _fake_build_http(_cfg, *, pool_maxsize=64):
      acfg = async_k8s_client.Configuration()
      acfg.host = f"http://127.0.0.1:{port}"
      acfg.connection_pool_maxsize = pool_maxsize
      return async_k8s_client.ApiClient(configuration=acfg)

    c_live = Cluster(
        ClusterConfig(name="c-live", namespace="ns1", in_cluster=True),
        api_client=MagicMock(),
    )
    c_live._injected_sync_api_client = False
    loop_errors: list[dict] = []

    with warnings.catch_warnings(record=True) as caught_w:
      warnings.simplefilter("always")
      with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent_sandbox_rl.cluster.build_async_api_client", _fake_build_http)

        # (a) Stopped loop with live pooled keep-alive socket -> cross-loop aclose() succeeds cleanly
        stopped_loop = asyncio.new_event_loop()
        stopped_loop.set_exception_handler(lambda _lp, ctx: loop_errors.append(ctx))

        async def _populate_pool():
          await c_live.get_async_sandbox_client()
          resp = await c_live._async_api_client.call_api(
              "/livez", "GET", _return_http_data_only=True, _preload_content=False
          )
          await resp.read()
          resp.release()
          conns = c_live._async_api_client.rest_client.pool_manager.connector._conns
          assert sum(len(v) for v in conns.values()) >= 1
          protos = [p for entries in conns.values() for p, _ in entries]
          return [p.transport.get_extra_info("socket") for p in protos]

        pooled_socks = stopped_loop.run_until_complete(_populate_pool())
        assert pooled_socks and all(s.fileno() >= 0 for s in pooled_socks)
        # Do NOT close stopped_loop yet: exercise the stopped-but-unclosed target_loop path!
        asyncio.run(c_live.aclose())
        assert all(s.fileno() == -1 for s in pooled_socks)
        stopped_loop.close()

        # (b) Closed loop (asyncio.run #1 -> asyncio.run #2) with live pooled keep-alive socket
        closed_socks: list[socket.socket] = []

        async def _run1_populate_and_rebuild_check():
          sc1 = await c_live.get_async_sandbox_client()
          core1 = await c_live.async_exec_core_api()
          # Exercise same-loop closed ClientSession rebuild on both sandbox_client and ws_api!
          await c_live._async_api_client.close()
          await c_live._async_ws_api_client.close()
          sc2 = await c_live.get_async_sandbox_client()
          core2 = await c_live.async_exec_core_api()
          assert sc2 is not sc1 and not c_live._async_api_client.rest_client.pool_manager.closed
          assert core2 is not core1 and not c_live._async_ws_api_client.rest_client.pool_manager.closed
          resp = await c_live._async_api_client.call_api(
              "/livez", "GET", _return_http_data_only=True, _preload_content=False
          )
          await resp.read()
          resp.release()
          conns = c_live._async_api_client.rest_client.pool_manager.connector._conns
          for entries in conns.values():
            for proto, _ in entries:
              closed_socks.append(proto.transport.get_extra_info("socket"))

        asyncio.run(_run1_populate_and_rebuild_check())
        assert closed_socks
        asyncio.run(c_live.get_async_sandbox_client())
        assert all(s.fileno() == -1 for s in closed_socks)
        asyncio.run(c_live.aclose())
        gc.collect()

    assert loop_errors == []
    bad_w = [
        w for w in caught_w
        if any(k in str(w.message) for k in ("Unclosed", "ResourceWarning", "destroyed"))
    ]
    assert bad_w == []
  finally:
    stop_srv.set()
    srv.close()
    srv_th.join(timeout=1.0)
    for s in accepted:
      with pytest.MonkeyPatch.context():
        try:
          s.close()
        except OSError:
          pass

  # 2) _helper_async_session_open & BatchWatchdog._batch_session_open detect closed owned helper
  async def _check_owned_helper_closed():
    acfg = async_k8s_client.Configuration()
    acfg.host = "http://127.0.0.1:9"
    owned_api = async_k8s_client.ApiClient(configuration=acfg)
    _ = owned_api.rest_client.pool_manager
    h = AsyncK8sHelper(api_client=owned_api)
    h._owns_api_client = True
    await h._ensure_initialized()
    fake_b = SimpleNamespace(_client=SimpleNamespace(k8s_helper=h))
    assert _helper_async_session_open(h) is True
    assert BatchWatchdog._batch_session_open(fake_b) is True
    await h.close()
    assert h._api_client is None
    assert _helper_async_session_open(h) is False
    assert BatchWatchdog._batch_session_open(fake_b) is False

  asyncio.run(_check_owned_helper_closed())

  # 3) Mid-flight RuntimeError("Session is closed") falls back cleanly in AsyncBatchClaims & AsyncSandboxFleet
  c_mf = _batch_cluster(make_cluster, "c-mf")
  c_mf.resources.delete_claim = MagicMock()
  pm_box = SimpleNamespace(closed=False)
  mf_helper = SimpleNamespace(
      _api_client=SimpleNamespace(rest_client=SimpleNamespace(pool_manager=pm_box))
  )

  async def _midflight_close_release():
    pm_box.closed = True
    raise RuntimeError("Session is closed")

  sync_released_ids: list[str] = []
  abc_mf = AsyncBatchClaims(BatchClaimConfig(), ready_timeout=30, labels={})
  abc_mf._sync_release_single_batch = lambda _cl, b, _lp, _lt: sync_released_ids.append(b.batch_id)  # type: ignore[method-assign]

  async def _check_abc_midflight():
    cur_lp = asyncio.get_running_loop()
    b_mf = SimpleNamespace(
        batch_id="b-midflight",
        namespace="ns1",
        _client=SimpleNamespace(k8s_helper=mf_helper),
        err=lambda: None,
        release=_midflight_close_release,
    )
    abc_mf._opened.append((c_mf, b_mf, cur_lp, threading.current_thread()))
    failed = await abc_mf.release_all()
    assert failed == {}
    assert sync_released_ids == ["b-midflight"]

    # Also test AsyncSandboxFleet.release_all() mid-flight session closure fallback
    pm_box.closed = False
    bulk_called: list[bool] = []

    class _MidflightBatch:
      def __init__(self) -> None:
        self.batch_id = "b-fl-midflight"
        self.namespace = "ns1"
        self._client = SimpleNamespace(k8s_helper=mf_helper)
        self._state = None
        self._connected: dict = {}

      def err(self):
        return None

      async def release_members(self):
        bulk_called.append(True)
        pm_box.closed = True
        raise RuntimeError("Session is closed")

      async def release_member(self, _member):
        raise RuntimeError("Session is closed")

    b_fl = _MidflightBatch()
    af_mf = AsyncSandboxFleet(
        FleetConfig(claim_mode="batch", fail_fast=FailFastPolicy(enabled=False)),
        registry=ClusterRegistry([c_mf]),
    )
    for idx in (1, 2):
      m = Member(
          claim_name=f"cl-mf-{idx}",
          warmpool="pool-1",
          state=MemberState.READY,
          sandbox_name=f"sb-{idx}",
          pod_name=f"pod-{idx}",
          pod_ips=("10.0.0.1",),
      )
      sb = MagicMock()
      sb.close_connection = AsyncMock()
      h_obj = SandboxHandle(
          task=Task(id=f"t-{idx}", image="img:1"),
          cluster_name=c_mf.name,
          claim_name=m.claim_name,
          sandbox_id=f"sb-{idx}",
          pod_name=f"pod-{idx}",
          hostname=f"sb-{idx}",
          pod_ip="10.0.0.1",
          sandbox=sb,
          _cluster=c_mf,
          _batch_claim=AsyncBatchClaim(
              batch=b_fl,
              member=m,
              expires_at=time.monotonic() + 60.0,
              cluster=c_mf,
              _loop=cur_lp,
              _loop_thread=threading.current_thread(),
          ),
      )
      c_mf.reserve_claim()
      af_mf._fleet._claims_reserved += 1
      af_mf._fleet._handles[id(h_obj)] = h_obj
    await af_mf.release_all()
    assert bulk_called == [True]
    assert af_mf.handles() == []
    assert c_mf.resources.delete_claim.call_count == 2
    await af_mf.aclose()

  asyncio.run(_check_abc_midflight())


@pytest.mark.asyncio
async def test_pipelined_batch_fresh_warm_credit_and_foreground_create_gate(
    make_cluster,
):
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  c = Cluster(
      ClusterConfig(name="pipe-c1", namespace="ns1"),
      api_client=MagicMock(),
      async_api_client=MagicMock(),
  )
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0

  events: list[tuple[str, object, float]] = []

  class _PipeAsyncBatch:
    def __init__(self, batch_id: str, groups, **_kwargs):
      self.batch_id = batch_id
      self.groups = list(groups)
      self.released = False
      self._no_active_creates = asyncio.Event()
      self._no_active_creates.set()
      self._next_create_at = 0.0

    def err(self):
      return None

    async def seed_checked_pools(self, ready_by_pool):
      events.append(("seed", sorted(ready_by_pool.keys()), time.monotonic()))

    async def precheck_pools(self, pools, timeout=None):
      events.append(("precheck", sorted(pools), time.monotonic()))

    async def acquire(self, pool: str, *, timeout: float, pod_annotations=None):
      self._no_active_creates.clear()
      await asyncio.sleep(0.03)
      events.append(("post_done", pool, time.monotonic()))
      self._no_active_creates.set()
      return Member(
          claim_name=f"cl-{pool}",
          warmpool=pool,
          state=MemberState.READY,
          sandbox_name=f"sb-{pool}",
          pod_name=f"pod-{pool}",
          pod_ips=("10.3.0.9",),
      )

    def get_member(self, claim_name: str):
      return None

    async def connect(self, member: Member):
      sb = MagicMock(
          claim_name=member.claim_name,
          sandbox_id=member.sandbox_name,
          is_active=True,
      )
      sb.close_connection = AsyncMock()
      return sb

    async def release_members(self):
      return None

    async def release_member(self, member):
      return None

    async def release(self):
      self.released = True

  fake_async_client = MagicMock()
  fake_async_client.claim_batch = AsyncMock(
      side_effect=lambda groups, **kw: _PipeAsyncBatch("pb-1", groups, **kw)
  )
  fake_async_client.close = AsyncMock()
  c._async_sandbox_client = fake_async_client
  c._async_loop = asyncio.get_running_loop()
  c._async_init_lock = asyncio.Lock()

  af = AsyncSandboxFleet(
      FleetConfig(
          claim_mode="batch",
          max_concurrent=4,
          window_size=2,
          fail_fast=FailFastPolicy(enabled=False),
      ),
      registry=ClusterRegistry([c]),
  )
  af.load_tasks(["img-0", "img-1", "img-2", "img-3"])

  def _fake_warm(imgs, **_kw):
    events.append(("warm", list(imgs), time.monotonic()))
    af._fleet._warmed.update({img: 1 for img in imgs})

  af._fleet.warm_images = MagicMock(side_effect=_fake_warm)
  af._fleet.unwarm_images = MagicMock()
  af._fleet.preflight = MagicMock()

  async def _dwell_proc(_t, h):
    events.append(("proc_start", h.claim_name, time.monotonic()))
    await asyncio.sleep(0.08)
    return h.pod_name

  out = await af.run(_dwell_proc, strategy="pipelined", concurrency=2)
  assert len(out) == 4
  # 1) Background warm of window 1 starts AFTER window 0's foreground POSTs complete:
  post0_times = [
      ts for kind, p, ts in events
      if kind == "post_done" and p in (af.config.pool_name("img-0"), af.config.pool_name("img-1"))
  ]
  warm1_time = next(ts for kind, imgs, ts in events if kind == "warm" and imgs == ["img-2", "img-3"])
  assert len(post0_times) == 2 and warm1_time >= max(post0_times) - 1e-4
  # 2) seed_checked_pools for window 1 is stamped right before window 1 acquires (NOT early during prefetch):
  seed1_time = next(
      ts for kind, pools, ts in events
      if kind == "seed" and pools == sorted([af.config.pool_name("img-2"), af.config.pool_name("img-3")])
  )
  proc1_times = [
      ts for kind, cl, ts in events
      if kind == "proc_start" and any(af.config.pool_name(x) in str(cl) for x in ("img-2", "img-3"))
  ]
  assert seed1_time > warm1_time + 0.02
  assert min(proc1_times) - seed1_time < 0.08
  await af.aclose()


def test_trust_env_skipped_only_when_no_proxy_or_netrc(monkeypatch, tmp_path):
  import urllib.request
  import agent_sandbox_rl.cluster as cluster_mod

  class _Session:
    def __init__(self):
      self._trust_env = True

  monkeypatch.setenv("HOME", str(tmp_path))
  monkeypatch.delenv("NETRC", raising=False)
  monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
  assert cluster_mod._env_configures_proxy_or_netrc() is False
  session = _Session()
  cluster_mod._skip_unused_trust_env(session)
  assert session._trust_env is False
  cluster_mod._skip_unused_trust_env(None)

  async def _real_session_trust_env() -> bool:
    import aiohttp

    async with aiohttp.ClientSession(trust_env=True) as real:
      cluster_mod._skip_unused_trust_env(real)
      return real.trust_env

  assert asyncio.run(_real_session_trust_env()) is False

  monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://proxy:3128"})
  session = _Session()
  cluster_mod._skip_unused_trust_env(session)
  assert session._trust_env is True

  monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
  monkeypatch.setenv("NETRC", str(tmp_path / "custom_netrc"))
  assert cluster_mod._env_configures_proxy_or_netrc() is True
  monkeypatch.delenv("NETRC")
  (tmp_path / ".netrc").write_text("machine example.invalid login a password b\n")
  assert cluster_mod._env_configures_proxy_or_netrc() is True

  def _boom():
    raise RuntimeError("proxy lookup failed")

  (tmp_path / ".netrc").unlink()
  monkeypatch.setattr(urllib.request, "getproxies", _boom)
  assert cluster_mod._env_configures_proxy_or_netrc() is True


def _cluster_with_mock_apis():
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  c = Cluster(ClusterConfig(name="c-reclaim", namespace="ns1", in_cluster=True), api_client=MagicMock())
  c.custom_api = MagicMock()
  c.core_api = MagicMock()
  return c


def test_delete_released_sandbox_deletes_sandbox_then_pod():
  c = _cluster_with_mock_apis()
  order: list[str] = []
  c.custom_api.delete_namespaced_custom_object.side_effect = lambda **kw: order.append("sandbox")
  c.core_api.delete_namespaced_pod.side_effect = lambda *a, **kw: order.append("pod")

  c.delete_released_sandbox("sb-1", "pod-1")
  c.delete_released_sandbox(None, "pod-2")  # claim never bound a sandbox: nothing to delete
  c.delete_released_sandbox("sb-3", None)   # no pod recorded: only the Sandbox is deleted
  c._released_sandbox_pool.shutdown(wait=True)

  assert sorted(order) == ["pod", "sandbox", "sandbox"]
  assert order.index("sandbox") < order.index("pod")
  first = c.custom_api.delete_namespaced_custom_object.call_args_list[0].kwargs
  assert (first["plural"], first["name"], first["namespace"]) == ("sandboxes", "sb-1", "ns1")
  assert c.core_api.delete_namespaced_pod.call_args.args[:2] == ("pod-1", "ns1")


def test_released_sandbox_deletes_stay_within_max_in_flight():
  from agent_sandbox_rl import cluster as cluster_mod

  c = _cluster_with_mock_apis()
  c.limit_released_sandbox_deletes(4)
  c.delete_released_sandbox("sb-1", None)
  assert c._released_sandbox_pool._max_workers == 4
  c._released_sandbox_pool.shutdown(wait=True)

  c = _cluster_with_mock_apis()
  c.limit_released_sandbox_deletes(1000)
  c.delete_released_sandbox("sb-1", None)
  assert c._released_sandbox_pool._max_workers == cluster_mod.RELEASED_SANDBOX_DELETE_WORKERS
  c._released_sandbox_pool.shutdown(wait=True)


def test_delete_released_sandbox_error_handling():
  from kubernetes.client import ApiException

  # A Sandbox that is already gone still has its pod deleted.
  c = _cluster_with_mock_apis()
  c.custom_api.delete_namespaced_custom_object.side_effect = ApiException(status=404)
  c.delete_released_sandbox("sb-gone", "pod-1")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c.core_api.delete_namespaced_pod.call_count == 1
  assert c._released_sandbox_disabled is False

  # A Sandbox whose delete failed may still exist, so its pod is left alone.
  c = _cluster_with_mock_apis()
  c.custom_api.delete_namespaced_custom_object.side_effect = ApiException(status=500)
  c.delete_released_sandbox("sb-1", "pod-1")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c.core_api.delete_namespaced_pod.call_count == 0
  assert c._released_sandbox_disabled is False

  # The first 403, on either object, turns the deletes off.
  for forbidden in ("sandbox", "pod"):
    c = _cluster_with_mock_apis()
    if forbidden == "sandbox":
      c.custom_api.delete_namespaced_custom_object.side_effect = ApiException(status=403)
    else:
      c.core_api.delete_namespaced_pod.side_effect = ApiException(status=403)
    c.delete_released_sandbox("sb-1", "pod-1")
    c._released_sandbox_pool.shutdown(wait=True)
    assert c._released_sandbox_disabled is True
    assert c.core_api.delete_namespaced_pod.call_count == (0 if forbidden == "sandbox" else 1)
    c.delete_released_sandbox("sb-2", "pod-2")  # nothing more is queued
    assert c.custom_api.delete_namespaced_custom_object.call_count == 1

  # Enough requests in a row that never reach the API server ...
  from agent_sandbox_rl import cluster as cluster_mod

  c = _cluster_with_mock_apis()
  c.custom_api.delete_namespaced_custom_object.side_effect = OSError("connection refused")
  for i in range(cluster_mod.RELEASED_SANDBOX_MAX_ERRORS + 5):
    c._delete_released_sandbox(f"sb-{i}", "pod")
  # ... pause them for a while, without turning them off for good.
  assert (c.custom_api.delete_namespaced_custom_object.call_count
          == cluster_mod.RELEASED_SANDBOX_MAX_ERRORS)
  assert c._released_sandbox_disabled is False
  c._released_sandbox_paused_until = 0.0
  c.custom_api.delete_namespaced_custom_object.side_effect = None
  c._delete_released_sandbox("sb-later", "pod")
  assert c.core_api.delete_namespaced_pod.call_count == 1

  # A response with a status, even a failure, means the API server is reachable.
  c = _cluster_with_mock_apis()
  for i in range(cluster_mod.RELEASED_SANDBOX_MAX_ERRORS - 1):
    c.custom_api.delete_namespaced_custom_object.side_effect = OSError("reset")
    c._delete_released_sandbox(f"sb-{i}", None)
  c.custom_api.delete_namespaced_custom_object.side_effect = ApiException(status=500)
  c._delete_released_sandbox("sb-500", None)
  c.custom_api.delete_namespaced_custom_object.side_effect = OSError("reset")
  c._delete_released_sandbox("sb-after", None)
  assert c._released_sandbox_paused_until == 0.0


def test_batch_handle_deletes_its_sandbox_only_once_the_claim_is_deleted():
  calls: list[tuple] = []
  cluster = types.SimpleNamespace(
      delete_released_sandbox=lambda sb, pod: calls.append((sb, pod)),
      _sandbox_client=None, _async_sandbox_client=None)

  def _handle():
    return types.SimpleNamespace(
        _cluster=cluster, claim_name="claim-1", sandbox=None, sandbox_id="sb-1", pod_name="pod-1",
        _batch_claim=None,
        delete_released_sandbox=lambda: SandboxHandle.delete_released_sandbox(h))

  h = _handle()
  SandboxHandle._cleanup_batch_after_remote_delete(h)
  assert calls == [("sb-1", "pod-1")]

  # Teardown finalizes handles before the batch's deletecollection has run.
  calls.clear()
  h = _handle()
  SandboxHandle._cleanup_batch_after_remote_delete(h, claim_deleted=False)
  assert calls == []
  SandboxHandle.delete_released_sandbox(h)
  assert calls == [("sb-1", "pod-1")]


# Paste at the end of examples/agent-sandbox-rl/tests/test_batch_claims.py.
# Uses names that file already defines or imports: FakeBatch (via _batch_cluster),
# _FakeAsyncBatch, _fleet, AsyncSandboxFleet, BatchError, ClusterRegistry,
# FailFastPolicy, FleetConfig, AsyncMock, MagicMock, asyncio, pytest.


def _record_teardown_order(cluster, events: list[tuple]) -> None:
  """Record this cluster's released-sandbox deletes in ``events``."""
  cluster.delete_released_sandbox = (
      lambda sandbox, pod: events.append(("delete_sandbox", sandbox, pod)))


def test_teardown_deletes_released_sandboxes_only_after_the_batch_claims(make_cluster):
  c = _batch_cluster(make_cluster)
  events: list[tuple] = []
  _record_teardown_order(c, events)
  f = _fleet(ClusterRegistry([c]), fail_fast=FailFastPolicy(enabled=False))
  f.load_tasks(["img"] * 3)
  f.plan()
  handles = [f.acquire(t) for t in f.tasks]
  batch = c.batches[0]
  delete_claims = batch.release

  def release():
    # A live claim whose Sandbox is gone adopts another warm sandbox, so nothing
    # may have been deleted by the time the batch starts deleting its claims.
    events.append(("claims_delete_started", len(events)))
    delete_claims()
    events.append(("claims_deleted",))
  batch.release = release

  f.teardown()

  assert events[0] == ("claims_delete_started", 0)
  assert events[1] == ("claims_deleted",)
  assert sorted(events[2:]) == sorted(
      ("delete_sandbox", h.sandbox_id, h.pod_name) for h in handles)
  assert batch.released_members == []            # the claims went with batch.release()


def test_teardown_keeps_sandboxes_of_a_batch_whose_release_failed(make_cluster):
  c = _batch_cluster(make_cluster)
  events: list[tuple] = []
  _record_teardown_order(c, events)
  f = _fleet(ClusterRegistry([c]), fail_fast=FailFastPolicy(enabled=False))
  f.load_tasks(["img"] * 3)
  f.plan()
  for t in f.tasks:
    f.acquire(t)
  c.batches[0].release = MagicMock(side_effect=BatchError("lease lost"))

  with pytest.raises(BatchError, match="lease lost"):
    f.teardown()

  c.batches[0].release.assert_called_once()
  assert events == []                            # the claims may still be live


def _native_async_batch_fleet(name: str):
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  c = Cluster(ClusterConfig(name=name, namespace="ns1"),
              api_client=MagicMock(), async_api_client=MagicMock())
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.ensure_template.return_value = "tmpl"
  c.resources.create_warmpool.return_value = True
  c.resources.wait_for_pool_ready.return_value = True
  for listing in ("list_claims", "list_leases", "list_warmpools", "list_templates"):
    getattr(c.resources, listing).return_value = []
  c.resources.count_pods.return_value = 0

  batches: list[_FakeAsyncBatch] = []

  async def claim_batch(groups, **kwargs):
    batches.append(_FakeAsyncBatch(f"{name}-{len(batches) + 1}", groups, **kwargs))
    return batches[-1]

  async_client = MagicMock()
  async_client.claim_batch = AsyncMock(side_effect=claim_batch)
  async_client.close = AsyncMock()
  c._async_sandbox_client = async_client
  c._async_loop = asyncio.get_running_loop()
  c._async_init_lock = asyncio.Lock()
  af = AsyncSandboxFleet(
      FleetConfig(claim_mode="batch", max_concurrent=8,
                  fail_fast=FailFastPolicy(enabled=False)),
      registry=ClusterRegistry([c]))
  return c, af, batches


async def test_async_teardown_deletes_released_sandboxes_only_after_the_batch_claims():
  c, af, batches = _native_async_batch_fleet("order-ok")
  events: list[tuple] = []
  _record_teardown_order(c, events)
  af.load_tasks(["img-a"] * 3)
  af._fleet.plan()
  handles = await af.acquire_batch(af.tasks)
  assert len(batches) == 1
  delete_claims = batches[0].release

  async def release():
    events.append(("claims_delete_started", len(events)))
    await delete_claims()
    events.append(("claims_deleted",))
  batches[0].release = release

  await af.teardown()
  await af.aclose()

  assert events[0] == ("claims_delete_started", 0)
  assert events[1] == ("claims_deleted",)
  assert sorted(events[2:]) == sorted(
      ("delete_sandbox", h.sandbox_id, h.pod_name) for h in handles)
  assert batches[0].released_members == []


async def test_async_teardown_keeps_sandboxes_of_a_batch_whose_release_failed():
  c, af, batches = _native_async_batch_fleet("order-fail")
  events: list[tuple] = []
  _record_teardown_order(c, events)
  af.load_tasks(["img-a"] * 3)
  af._fleet.plan()
  await af.acquire_batch(af.tasks)
  batches[0].release = AsyncMock(side_effect=BatchError("lease lost"))

  with pytest.raises(BatchError, match="lease lost"):
    await af.teardown()
  await af.aclose()

  assert batches[0].release.await_count >= 1
  assert events == []


def test_exec_retries_a_connection_that_was_never_established():
  import aiohttp
  from kubernetes_asyncio import client as async_k8s_client
  from kubernetes_asyncio.stream.ws_client import STDOUT_CHANNEL

  from agent_sandbox_rl import cluster as cluster_mod
  from agent_sandbox_rl.cluster import _build_fast_ws_core_api
  from agent_sandbox_rl.config import ClusterConfig

  url = "https://127.0.0.1:6443/api/v1/namespaces/ns1/pods/p/exec"

  class _Ctx:
    def __init__(self, enter_error=None, stream_error=None):
      self.enter_error, self.stream_error = enter_error, stream_error

    async def __aenter__(self):
      if self.enter_error is not None:
        raise self.enter_error
      stream_error = self.stream_error

      async def _gen():
        yield types.SimpleNamespace(data=bytes([STDOUT_CHANNEL]) + b"ok")
        if stream_error is not None:
          raise stream_error
      return _gen()

    async def __aexit__(self, *exc):
      return False

  async def _run(outcomes):
    base_cfg = async_k8s_client.Configuration()
    base_cfg.host = "https://127.0.0.1:6443"
    ws_api, _core = await _build_fast_ws_core_api(
        ClusterConfig(name="c-ws", namespace="ns1", in_cluster=True),
        pool_maxsize=32, base_configuration=base_cfg)
    calls = []
    try:
      assert ws_api.rest_client.pool_manager.timeout.sock_connect == (
          cluster_mod.EXEC_CONNECT_TIMEOUT_SECONDS)

      def _connect(*a, **kw):
        calls.append(1)
        return outcomes[min(len(calls), len(outcomes)) - 1]
      ws_api.rest_client.pool_manager.ws_connect = _connect
      try:
        return (await ws_api.request("GET", url)).data, len(calls)
      except Exception as exc:  # noqa: BLE001
        return exc, len(calls)
    finally:
      await ws_api.close()

  not_established = aiohttp.ConnectionTimeoutError("Connection timeout to host")

  # Two lost connections, then one that works: the command runs once.
  data, calls = asyncio.run(_run([_Ctx(enter_error=not_established)] * 2 + [_Ctx()]))
  assert (data, calls) == (b"ok", 3)

  # It gives up after EXEC_CONNECT_ATTEMPTS and reports the connection error.
  exc, calls = asyncio.run(_run([_Ctx(enter_error=not_established)]))
  assert isinstance(exc, aiohttp.ConnectionTimeoutError)
  assert calls == cluster_mod.EXEC_CONNECT_ATTEMPTS

  # A failure after the connection is up may have run the command: no retry.
  exc, calls = asyncio.run(_run([_Ctx(stream_error=not_established)]))
  assert isinstance(exc, aiohttp.ConnectionTimeoutError)
  assert calls == 1


def test_released_sandboxes_with_claim_uids_are_deleted_together_by_label(monkeypatch):
  from kubernetes.client import ApiException

  from agent_sandbox_rl import cluster as cluster_mod

  monkeypatch.setattr(cluster_mod, "RELEASED_SANDBOX_BATCH_WINDOW_SECONDS", 0.05)
  monkeypatch.setattr(cluster_mod, "RELEASED_SANDBOX_BATCH_SIZE", 3)

  def _labelled(uid):
    return {"metadata": {"labels": {cluster_mod.CLAIM_UID_LABEL: uid}}}

  # The controller labels Sandboxes: 5 releases go out as 2 batches of 2 requests each.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.return_value = _labelled("uid-0")
  for i in range(5):
    c.delete_released_sandbox(f"sb-{i}", f"pod-{i}", claim_uid=f"uid-{i}")
  c._released_sandbox_pool.shutdown(wait=True)
  sandbox_selectors = [k.kwargs["label_selector"]
                       for k in c.custom_api.delete_collection_namespaced_custom_object.call_args_list]
  pod_selectors = [k.kwargs["label_selector"]
                   for k in c.core_api.delete_collection_namespaced_pod.call_args_list]
  # Batches run side by side, so only their contents are fixed, not their order.
  assert sorted(sandbox_selectors) == sorted(pod_selectors) == [
      "agents.x-k8s.io/claim-uid in (uid-0,uid-1,uid-2)",
      "agents.x-k8s.io/claim-uid in (uid-3,uid-4)",
  ]
  assert c.custom_api.get_namespaced_custom_object.call_count == 1
  assert c.custom_api.delete_namespaced_custom_object.call_count == 0
  assert c.core_api.delete_namespaced_pod.call_count == 0

  # A controller that does not label Sandboxes: deleting pods by label would leave
  # each Sandbox to start a replacement, so every release is deleted by name.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.return_value = {"metadata": {"labels": {}}}
  for i in range(2):
    c.delete_released_sandbox(f"sb-{i}", f"pod-{i}", claim_uid=f"uid-{i}")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c.custom_api.delete_collection_namespaced_custom_object.call_count == 0
  assert c.core_api.delete_collection_namespaced_pod.call_count == 0
  assert c.custom_api.delete_namespaced_custom_object.call_count == 2
  assert c.core_api.delete_namespaced_pod.call_count == 2

  # No deletecollection access: fall back to deleting by name, and stay there.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.return_value = _labelled("uid-0")
  c.custom_api.delete_collection_namespaced_custom_object.side_effect = ApiException(status=403)
  c.delete_released_sandbox("sb-0", "pod-0", claim_uid="uid-0")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c.core_api.delete_collection_namespaced_pod.call_count == 0
  assert c.custom_api.delete_namespaced_custom_object.call_count == 1
  assert c._released_sandbox_by_label is False
  assert c._released_sandbox_disabled is False

  # A failed Sandbox request leaves the pods alone.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.return_value = _labelled("uid-0")
  c.custom_api.delete_collection_namespaced_custom_object.side_effect = ApiException(status=500)
  c.delete_released_sandbox("sb-0", "pod-0", claim_uid="uid-0")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c.core_api.delete_collection_namespaced_pod.call_count == 0
  assert c.core_api.delete_namespaced_pod.call_count == 0


def _release_and_wait_for_flush(c, i):
  c.delete_released_sandbox(f"sb-{i}", f"pod-{i}", claim_uid=f"uid-{i}")
  deadline = time.monotonic() + 5
  while c._released_sandbox_flush_queued and time.monotonic() < deadline:
    time.sleep(0.005)
  assert c._released_sandbox_flush_queued is False


def test_released_sandbox_label_probe_decides_when_the_first_sandbox_is_gone(monkeypatch):
  from kubernetes.client import ApiException

  from agent_sandbox_rl import cluster as cluster_mod

  monkeypatch.setattr(cluster_mod, "RELEASED_SANDBOX_BATCH_WINDOW_SECONDS", 0.05)

  def _get(present):
    def get(**kw):
      if kw["name"] not in present:
        raise ApiException(status=404)
      return {"metadata": {"labels": present[kw["name"]]}}
    return get

  # The garbage collector already removed the first released Sandbox; the next
  # one of the batch still shows the label.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.side_effect = _get(
      {"sb-1": {cluster_mod.CLAIM_UID_LABEL: "uid-1"}})
  for i in range(3):
    c.delete_released_sandbox(f"sb-{i}", f"pod-{i}", claim_uid=f"uid-{i}")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c._released_sandbox_by_label is True
  assert [k.kwargs["name"] for k in c.custom_api.get_namespaced_custom_object.call_args_list] == [
      "sb-0", "sb-1"]
  assert c.custom_api.list_namespaced_custom_object.call_count == 0
  assert c.custom_api.delete_collection_namespaced_custom_object.call_count == 1
  assert c.custom_api.delete_namespaced_custom_object.call_count == 0

  # The next one exists without the label: this controller does not set it.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.side_effect = _get({"sb-1": {}})
  for i in range(3):
    c.delete_released_sandbox(f"sb-{i}", f"pod-{i}", claim_uid=f"uid-{i}")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c._released_sandbox_by_label is False
  assert c.custom_api.list_namespaced_custom_object.call_count == 0
  assert c.custom_api.delete_collection_namespaced_custom_object.call_count == 0
  assert c.core_api.delete_collection_namespaced_pod.call_count == 0
  assert c.custom_api.delete_namespaced_custom_object.call_count == 3

  # Every candidate is gone, but another claim's Sandbox in the namespace carries
  # the label. Only three of the batch are read before the one list.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
  c.custom_api.list_namespaced_custom_object.return_value = {
      "items": [{"metadata": {"labels": {cluster_mod.CLAIM_UID_LABEL: "other"}}}]}
  for i in range(5):
    c.delete_released_sandbox(f"sb-{i}", f"pod-{i}", claim_uid=f"uid-{i}")
  c._released_sandbox_pool.shutdown(wait=True)
  assert c._released_sandbox_by_label is True
  assert c.custom_api.get_namespaced_custom_object.call_count == 3
  listed = c.custom_api.list_namespaced_custom_object.call_args
  assert listed.kwargs["label_selector"] == "agents.x-k8s.io/claim-uid"
  assert listed.kwargs["limit"] == 1
  assert listed.kwargs["namespace"] == "ns1"
  assert c.custom_api.delete_collection_namespaced_custom_object.call_count == 1
  assert c.custom_api.delete_namespaced_custom_object.call_count == 0


def test_released_sandbox_label_probe_backs_off_while_undecided(monkeypatch):
  from kubernetes.client import ApiException

  from agent_sandbox_rl import cluster as cluster_mod

  monkeypatch.setattr(cluster_mod, "RELEASED_SANDBOX_BATCH_WINDOW_SECONDS", 0.005)
  # Every released Sandbox is gone and no Sandbox carries the label: nothing to
  # decide on, so each batch is deleted by name and the probe runs on the 1st,
  # 2nd, 4th and 8th of 8 batches instead of on every one.
  c = _cluster_with_mock_apis()
  c.custom_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
  c.custom_api.list_namespaced_custom_object.return_value = {"items": []}
  for i in range(8):
    _release_and_wait_for_flush(c, i)
  assert c._released_sandbox_by_label is None
  assert c.custom_api.get_namespaced_custom_object.call_count == 4
  assert c.custom_api.list_namespaced_custom_object.call_count == 4
  # No list access leaves the question open; it does not turn the deletes off.
  c.custom_api.list_namespaced_custom_object.side_effect = ApiException(status=403)
  for i in range(8, 16):
    _release_and_wait_for_flush(c, i)
  c._released_sandbox_pool.shutdown(wait=True)
  assert c.custom_api.list_namespaced_custom_object.call_count == 5
  assert c._released_sandbox_by_label is None
  assert c._released_sandbox_disabled is False
  assert c.custom_api.delete_collection_namespaced_custom_object.call_count == 0
  assert c.core_api.delete_collection_namespaced_pod.call_count == 0
  assert c.custom_api.delete_namespaced_custom_object.call_count == 16
  assert c.core_api.delete_namespaced_pod.call_count == 16


def test_released_sandbox_403_log_names_the_object(caplog):
  from kubernetes.client import ApiException

  c = _cluster_with_mock_apis()
  c.custom_api.delete_namespaced_custom_object.side_effect = ApiException(status=403)
  with caplog.at_level("INFO", logger="agent_sandbox_rl.cluster"):
    c.delete_released_sandbox("sb-1", "pod-1")
    c._released_sandbox_pool.shutdown(wait=True)
  assert "no delete access to released Sandboxes;" in caplog.text


def test_batch_handle_passes_its_claim_uid_to_the_released_sandbox_delete():
  calls: list[tuple] = []
  cluster = types.SimpleNamespace(
      delete_released_sandbox=lambda sb, pod, **kw: calls.append((sb, pod, kw)))
  handle = types.SimpleNamespace(
      _cluster=cluster, sandbox_id="sb-1", pod_name="pod-1",
      _batch_claim=types.SimpleNamespace(member=types.SimpleNamespace(claim_uid="uid-1")))
  SandboxHandle.delete_released_sandbox(handle)
  handle._batch_claim = types.SimpleNamespace(member=types.SimpleNamespace(claim_uid=None))
  SandboxHandle.delete_released_sandbox(handle)
  assert calls == [("sb-1", "pod-1", {"claim_uid": "uid-1"}), ("sb-1", "pod-1", {})]


def test_released_sandbox_flusher_recovers_after_it_raises(monkeypatch):
  from agent_sandbox_rl import cluster as cluster_mod

  monkeypatch.setattr(cluster_mod, "RELEASED_SANDBOX_BATCH_WINDOW_SECONDS", 0.01)
  c = _cluster_with_mock_apis()
  probes: list[str] = []

  def probe(batch):
    probes.append(batch[0][2])
    if len(probes) == 1:
      raise TypeError("unexpected")
    return True
  c._sandbox_has_claim_uid_label = probe

  c.delete_released_sandbox("sb-0", "pod-0", claim_uid="uid-0")
  deadline = time.monotonic() + 5
  while (not probes or c._released_sandbox_flush_queued) and time.monotonic() < deadline:
    time.sleep(0.01)
  # The failed flusher must not leave later releases queued behind a flag nobody clears.
  assert probes == ["uid-0"]
  assert c._released_sandbox_flush_queued is False

  c.delete_released_sandbox("sb-1", "pod-1", claim_uid="uid-1")
  deadline = time.monotonic() + 5
  while c._released_sandbox_flush_queued and time.monotonic() < deadline:
    time.sleep(0.01)
  c._released_sandbox_pool.shutdown(wait=True)
  assert probes == ["uid-0", "uid-1"]
  assert c.core_api.delete_collection_namespaced_pod.call_count == 1
  assert c._released_sandbox_pending == []


def test_release_all_does_not_bulk_release_while_an_acquire_is_in_flight(make_cluster):
  c = _batch_cluster(make_cluster)
  f = _fleet(ClusterRegistry([c]), max_concurrent=8,
             fail_fast=FailFastPolicy(enabled=False))
  tasks = f.load_tasks(["img-0", "img-1", "img-2"])
  f.plan()
  held = [f.acquire(tasks[0]), f.acquire(tasks[1])]
  b = c.batches[0]
  real_acquire = b.acquire
  entered, gate = threading.Event(), threading.Event()

  def held_acquire(warmpool, timeout=None, *, pod_annotations=None):
    entered.set()
    assert gate.wait(10)
    return real_acquire(warmpool, timeout=timeout, pod_annotations=pod_annotations)

  b.acquire = held_acquire
  with ThreadPoolExecutor(max_workers=1) as ex:
    in_flight = ex.submit(f.acquire, tasks[2])
    assert entered.wait(10)
    # release_members() would release the whole batch and fail the acquire in flight.
    f.release_all()
    assert b.release_members_calls == 0
    assert sorted(b.released_members) == sorted(h.claim_name for h in held)
    gate.set()
    late = in_flight.result(10)
  assert [h.claim_name for h in f.handles()] == [late.claim_name]
  assert f._claims_reserved == 1

  # With no acquire in flight the same call uses one bulk release.
  b.acquire = real_acquire
  f.acquire(tasks[0])
  f.release_all()
  assert b.release_members_calls == 1
  assert f.handles() == [] and f._claims_reserved == 0
  f.teardown()


async def test_async_release_does_not_bulk_release_while_an_acquire_is_in_flight(monkeypatch):
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  monkeypatch.setattr("agent_sandbox_rl.preflight.preflight_cluster",
                      lambda cluster, **kw: MagicMock(ok=True, checks=[]))
  c = Cluster(ClusterConfig(name="c-in-flight", namespace="ns1", in_cluster=True),
              api_client=MagicMock(),
              async_api_client=MagicMock())
  c.resources = MagicMock()
  c.resources.labels = {}
  c.resources.namespace = "ns1"
  c.resources.list_claims.return_value = []
  c.resources.list_leases.return_value = []
  c.resources.list_warmpools.return_value = []
  c.resources.list_templates.return_value = []
  c.resources.count_pods.return_value = 0

  async_batches: list[_FakeAsyncBatch] = []

  async def _claim_batch(groups, **kwargs):
    async_batches.append(_FakeAsyncBatch("ab-1", groups, **kwargs))
    return async_batches[-1]

  fake_async_client = MagicMock()
  fake_async_client.claim_batch = AsyncMock(side_effect=_claim_batch)
  fake_async_client.close = AsyncMock()
  c._async_sandbox_client = fake_async_client
  c._async_loop = asyncio.get_running_loop()
  c._async_init_lock = asyncio.Lock()

  af = AsyncSandboxFleet(
      FleetConfig(claim_mode="batch", max_concurrent=4,
                  fail_fast=FailFastPolicy(enabled=False)),
      registry=ClusterRegistry([c]),
  )
  tasks = af.load_tasks(["img-a", "img-b", "img-c"])
  af._fleet.plan()
  for e in af._fleet.plan_.entries:
    af._fleet._warmed[e.image] = 1
  await af._prepare_async_batch_clusters(af._fleet.plan_.entries, just_warmed=True)
  b = async_batches[0]
  real_acquire = b.acquire

  async def _hold_next_acquire():
    entered, gate = asyncio.Event(), asyncio.Event()

    async def held_acquire(warmpool, timeout=None, *, pod_annotations=None):
      b.acquire = real_acquire
      entered.set()
      await gate.wait()
      return await real_acquire(warmpool, timeout=timeout, pod_annotations=pod_annotations)

    b.acquire = held_acquire
    in_flight = asyncio.create_task(af.acquire(tasks[2]))
    await asyncio.wait_for(entered.wait(), 5)
    return in_flight, gate

  # release_members() would release the whole batch and fail the acquire in flight.
  held = [await af.acquire(tasks[0]), await af.acquire(tasks[1])]
  in_flight, gate = await _hold_next_acquire()
  await af.release_all()
  assert b.release_members_calls == 0
  assert sorted(b.released_members) == sorted(h.claim_name for h in held)
  gate.set()
  late = await asyncio.wait_for(in_flight, 5)
  assert [h.claim_name for h in af.handles()] == [late.claim_name]
  await af.release(late)

  # The release at the end of a processed wave takes the same care.
  b.released_members.clear()
  in_flight, gate = await _hold_next_acquire()
  out = await af._process_parallel(tasks[:2], lambda _t, h: h.claim_name, 2)
  assert b.release_members_calls == 0
  assert sorted(b.released_members) == sorted(out)
  gate.set()
  late = await asyncio.wait_for(in_flight, 5)
  await af.release(late)

  # With no acquire in flight both paths use one bulk release.
  await af.acquire(tasks[0])
  await af.acquire(tasks[1])
  await af.release_all()
  assert b.release_members_calls == 1
  await af._process_parallel(tasks[:2], lambda _t, h: h.claim_name, 2)
  assert b.release_members_calls == 2
  assert af.handles() == [] and af._fleet._claims_reserved == 0
  await af.aclose()
