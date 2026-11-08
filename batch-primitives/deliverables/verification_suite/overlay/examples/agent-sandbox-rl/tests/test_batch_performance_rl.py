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

"""Deterministic performance and complexity invariants for agent-sandbox-rl batch claiming.

Counts executed Python bytecode instructions and peak allocated bytes (machine independent,
unlike wall-clock time) for per-worker operations at a small and a large fleet, asserts that
the liveness, BatchClaim, batch-mode acquire, and idle-watchdog paths send no Kubernetes
request, pins repeat on-demand acquires to their minimum number of requests (pod reads included),
bounds the memory a long-lived fleet retains per episode with run recording off and on, and
requires session command output to cost time linear in its size. See the SDK counterpart
``k8s_agent_sandbox/test/unit/test_batch_performance_invariants.py`` for the counting method.

Behavior already pinned elsewhere is not repeated here: ``test_batch_claims.py`` covers the
zero-GET ``is_alive()`` on claim loss (WATCH-10) and the single ``size=0`` seed group on the
first acquire (PERF-20).
"""

from __future__ import annotations

import gc
import os
import time
import pathlib
import subprocess
import sys
import threading
import tracemalloc
import types
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client as k8s_client

from k8s_agent_sandbox.batch_state import BatchState
from k8s_agent_sandbox.constants import (BATCH_GROUP_MIN_READY_ANNOTATION,
                                         BATCH_GROUP_SIZE_ANNOTATION)
from k8s_agent_sandbox.models import BatchGroup
from k8s_agent_sandbox.sandbox_batch import SandboxBatch
from k8s_agent_sandbox.sandbox_client import SandboxClient

from agent_sandbox_rl import BatchClaimConfig, ClusterRegistry, FleetConfig, SandboxFleet
from agent_sandbox_rl.batch_claims import BatchClaim, BatchClaims
from agent_sandbox_rl.handles import SandboxHandle, SandboxSession
from agent_sandbox_rl.sources import Task

O1_MAX_RATIO = 1.15
MIN_PLAUSIBLE_COUNT = 20
# Absolute, so a whole-collection copy can't hide inside a large constant peak; measured noise
# between sizes is under 600 B.
PEAK_SLACK_BYTES = 4096


def count_bytecode_instructions(fn: Callable[[], object]) -> int:
  """Returns the number of Python bytecode instructions the calling thread executes in
  ``fn()`` (``sys.monitoring`` INSTRUCTION events on 3.12+, ``sys.settrace`` opcode events
  on 3.11). Instructions from other threads are dropped, since ``sys.monitoring`` is
  process-wide. Kept local rather than imported from the SDK's test package, so this suite
  only depends on the SDK's public modules."""
  count = 0
  owner = threading.get_ident()
  monitoring: Any = getattr(sys, "monitoring", None)
  if monitoring is not None:
    for tool_id in (5, 4, 3, 2, 0):
      try:
        monitoring.use_tool_id(tool_id, "rl_batch_perf_invariants")
      except ValueError:
        continue
      instruction = monitoring.events.INSTRUCTION

      def on_instruction(_code: object, _offset: int) -> None:
        nonlocal count
        if threading.get_ident() == owner:
          count += 1

      monitoring.register_callback(tool_id, instruction, on_instruction)
      monitoring.set_events(tool_id, instruction)
      try:
        fn()
      finally:
        monitoring.set_events(tool_id, 0)
        monitoring.register_callback(tool_id, instruction, None)
        monitoring.free_tool_id(tool_id)
      return count
    raise RuntimeError("no free sys.monitoring tool id")

  def tracer(frame: Any, event: str, _arg: object) -> Any:
    nonlocal count
    # f_trace must be set before f_trace_opcodes takes effect.
    frame.f_trace = tracer
    frame.f_trace_opcodes = True
    frame.f_trace_lines = False
    if event == "opcode":
      count += 1
    return tracer

  previous = sys.gettrace()
  sys.settrace(tracer)
  try:
    fn()
  finally:
    sys.settrace(previous)
  return count


class _Walks:
  """Whole-table walks seen during measure()'s counted runs. A walk in C (``max(d.values())``,
  ``s1 <= s2``, ``x in a_list``) executes no bytecode and allocates nothing, so the tables that
  grow with a gate's size are swapped for counting subclasses (``_count_walks``)."""
  count = 0
  measured: list[int] = []


def _counting(base: type, names: tuple[str, ...]) -> type:
  cls = type(f"_WalkCounting{base.__name__.title()}", (base,), {})
  for name in names:
    def walking(self: Any, *args: Any, _name: str = name, **kwargs: Any) -> Any:
      _Walks.count += 1
      return getattr(base, _name)(self, *args, **kwargs)

    setattr(cls, name, walking)
  return cls


# Only operations whose cost grows with the container itself (`s.update(x)` walks x).
_COUNTING: dict[type, type] = {
    dict: _counting(dict, ("__iter__", "keys", "values", "items", "copy", "__or__", "__eq__")),
    set: _counting(set, ("__iter__", "copy", "issubset", "__le__", "__lt__", "__eq__", "union",
                         "__or__", "difference", "__sub__")),
    list: _counting(list, ("__iter__", "copy", "__contains__", "index", "count", "remove",
                           "__eq__")),
}


def _count_walks(*owners: Any, at_least: int) -> None:
  """Swaps every plain dict, set, or list attribute of ``owners`` holding at least
  ``at_least`` entries for a walk-counting copy."""
  for owner in owners:
    for attr, value in list(vars(owner).items()):
      counting = _COUNTING.get(type(value))
      if counting is not None and len(value) >= at_least:
        setattr(owner, attr, counting(value))


def measure(build: Callable[[], Callable[[], object]], repeats: int = 3) -> int:
  """Warms the operation up on one fresh build, then returns the minimum count over
  ``repeats`` fresh builds, recording the whole-table walks each counted run made."""
  build()()
  counts = []
  for _ in range(repeats):
    fn = build()
    _Walks.count = 0
    counts.append(count_bytecode_instructions(fn))
    _Walks.measured.append(_Walks.count)
  return min(counts)


def measure_peak(build: Callable[[], Callable[[], object]], repeats: int = 3) -> int:
  """Warms the operation up, then returns its minimum peak allocation in bytes over
  ``repeats`` fresh builds. C builtins (dict(), sorted(), list copies) walk a collection
  without executing bytecode, but copying or sorting N entries allocates O(N) bytes."""
  build()()
  peaks = []
  for _ in range(repeats):
    fn = build()
    gc.collect()
    started = not tracemalloc.is_tracing()
    if started:
      tracemalloc.start()
    # Never clear_traces(): it hangs under pytest with tracing enabled from startup.
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    try:
      fn()
      peaks.append(tracemalloc.get_traced_memory()[1] - base)
    finally:
      if started:
        tracemalloc.stop()
  return min(peaks)


def assert_o1(label: str, build: Callable[[int], Callable[[], object]],
              sizes: tuple[int, int]) -> None:
  """Asserts flat instruction counts, flat peak allocation, and no whole-table walks between
  the two sizes."""
  _Walks.measured.clear()
  small = measure(lambda: build(sizes[0]))
  large = measure(lambda: build(sizes[1]))
  assert_scaling(label, small, large, sizes)
  walks, _Walks.measured[:] = list(_Walks.measured), []
  assert sum(walks) == 0, f"{label}: walked a whole table {walks} times across the measured runs"
  small_peak = measure_peak(lambda: build(sizes[0]))
  large_peak = measure_peak(lambda: build(sizes[1]))
  assert large_peak - small_peak <= PEAK_SLACK_BYTES, (
      f"{label}: peak {small_peak:,} B at {sizes[0]} vs {large_peak:,} B at {sizes[1]} "
      f"(a C-level copy or sort of the whole collection)")


def assert_scaling(label: str, small: int, large: int, sizes: tuple[int, int]) -> None:
  assert small >= MIN_PLAUSIBLE_COUNT, f"{label}: counted {small} instructions; broken counter"
  assert large / small <= O1_MAX_RATIO, (
      f"{label}: {small:,} instructions at {sizes[0]} vs {large:,} at {sizes[1]} "
      f"(ratio {large / small:.2f}x)")


def _acquired_state(n: int, pool: str = "pool-0") -> BatchState:
  """Returns a dynamic ``BatchState`` with ``n`` acquired Ready members in ``pool``."""
  state = BatchState("bperf", [BatchGroup(warmpool=pool, size=0)])
  _seed_acquired(state, n, pool)
  _count_walks(state, at_least=n)
  return state


def _seed_acquired(state: BatchState, n: int, pool: str) -> None:
  for i in range(n):
    name = state.start_acquire(pool)
    state.upsert_claim({
        "metadata": {
            "name": name,
            "uid": f"uid-{i}",
            "resourceVersion": str(i + 1),
            "annotations": {
                BATCH_GROUP_SIZE_ANNOTATION: "0",
                BATCH_GROUP_MIN_READY_ANNOTATION: "0",
            },
        },
        "spec": {"warmPoolRef": {"name": pool}},
        "status": {
            "sandbox": {"name": f"sbx-{i}", "podIPs": [f"10.1.{i // 256}.{i % 256}"]},
            "conditions": [{"type": "Ready", "status": "True", "reason": "SandboxReady"}],
        },
    })
    assert state.acquired_member(name) is not None


def _state_backed_batch(state: BatchState) -> Any:
  """A batch whose snapshot and by-name reads go through a real ``BatchState``, the
  way ``SandboxBatch.members()`` reads it under the batch lock."""
  return types.SimpleNamespace(
      err=lambda: None,
      members=state.members,
      get_member=state.get_member,
      batch_id=state.batch_id,
  )


def _route_pod_reads(cluster: Any) -> None:
  """Routes the handles' pod reads, which go through ``cluster.exec_core_api()``, to the
  recorded ``cluster.core_api`` mock. The shared FakeCluster has no ``exec_core_api``, and the
  handles swallow the AttributeError as a failed best-effort read, which would hide every pod
  GET from the request counts."""
  cluster.exec_core_api = lambda: cluster.core_api


def _cluster_requests(cluster: Any) -> list[str]:
  """The Kubernetes requests the fleet sent itself (not calls on the mocks they return)."""
  return [name for api in (cluster.resources, cluster.custom_api, cluster.core_api)
          for name, _args, _kwargs in api.method_calls]


def _real_batch(n: int) -> tuple[Any, BatchState, Any]:
  """Returns ``(batch, its state, its K8s helper mock)`` for a real ``SandboxBatch``
  with ``n`` acquired Ready members in ``pool-0``, so the handle's own locking and
  lookups are measured rather than a stand-in's. The K8s helper is a mock and the
  batch's watch, Lease renewal, and create worker never start."""
  with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"), \
      patch("k8s_agent_sandbox.sandbox_client.atexit.register"):
    sandbox_client: Any = SandboxClient()
  helper = sandbox_client.k8s_helper
  helper.api_pool_size.return_value = 25
  helper.count_live_sandbox_claims.return_value = 0
  helper.get_sandbox_warmpool.return_value = {
      "spec": {"sandboxTemplateRef": {"name": "tmpl"}}, "status": {"readyReplicas": 0}}
  helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
  helper.create_batch_lease.return_value = k8s_client.V1Lease(
      metadata=k8s_client.V1ObjectMeta(name="batch-bperf", resource_version="1", uid="u"),
      spec=k8s_client.V1LeaseSpec(holder_identity="h", lease_duration_seconds=3600,
                                  renew_time=datetime.now(timezone.utc)))
  helper.list_sandbox_claim_objects.return_value = ([], "1")
  with patch.object(SandboxBatch, "_start", lambda _self: None), \
      patch.object(SandboxBatch, "_create_worker", lambda _self: None):
    batch = sandbox_client.claim_batch([BatchGroup(warmpool="pool-0", size=0)],
                                       batch_id="bperf", lease_duration=3600)
  state = cast(BatchState, batch._state)
  _seed_acquired(state, n, "pool-0")
  _count_walks(state, batch, at_least=n)
  assert batch.err() is None
  helper.reset_mock()
  return batch, state, helper


def _backed_batch(n: int, backing: str) -> tuple[Any, BatchState, list[Any]]:
  """Returns ``(batch, state, requests)``: a stand-in reading ``state`` directly, or a
  real ``SandboxBatch``; ``requests`` collects the real batch's Kubernetes requests."""
  if backing == "handle":
    batch, state, helper = _real_batch(n)
    return batch, state, helper.method_calls
  state = _acquired_state(n)
  return _state_backed_batch(state), state, []


BACKINGS = ("state", "handle")


@pytest.mark.parametrize("backing", BACKINGS)
def test_batch_claim_still_reaches_is_o1_in_pool_size(backing: str) -> None:
  # F-30: SandboxHandle.is_alive() calls still_reaches() at every RL step boundary.
  # A lookup through batch.members(warmpool) copies and sorts the whole pool under
  # the batch lock on each call.
  sizes = (50, 2000)

  def build(n: int) -> Callable[[], object]:
    batch, state, requests = _backed_batch(n, backing)
    member = state.members("pool-0")[-1]
    cluster = MagicMock()
    claim = BatchClaim(batch=batch, member=member, expires_at=1e12, cluster=cluster)

    def run() -> None:
      assert claim.still_reaches() is True
      assert cluster.mock_calls == [] and requests == [], (
          "still_reaches() sent Kubernetes requests")

    return run

  assert_o1(f"BatchClaim.still_reaches ({backing})", build, sizes)


@pytest.mark.parametrize("backing", BACKINGS)
def test_handle_is_alive_on_lost_claim_is_o1_in_pool_size(backing: str) -> None:
  # is_alive() answers False from the batch watch alone once the member is gone, so
  # its cost must stay flat as the pool grows.
  sizes = (50, 2000)

  def build(n: int) -> Callable[[], object]:
    batch, state, requests = _backed_batch(n, backing)
    member = state.members("pool-0")[-1]
    state.release_claim(member.claim_name, "pool-0")
    assert member.sandbox_name is not None
    cluster = MagicMock()
    handle = SandboxHandle(
        task=Task(id="t", image="img"), cluster_name="c",
        claim_name=member.claim_name, sandbox_id=member.sandbox_name,
        pod_name=member.sandbox_name, hostname=member.sandbox_name,
        _cluster=cluster,
        _batch_claim=BatchClaim(batch=batch, member=member, expires_at=1e12,
                                cluster=cluster))

    def run() -> None:
      assert handle.is_alive() is False
      assert cluster.mock_calls == [] and requests == [], (
          "is_alive() on a lost claim sent requests")

    return run

  assert_o1(f"SandboxHandle.is_alive (lost claim, {backing})", build, sizes)


def test_batch_claim_release_sends_only_the_member_release() -> None:
  # Batch mode releases each episode's claim through the batch, which owns the delete;
  # BatchClaim.release must send nothing of its own.
  state = _acquired_state(10)
  batch = _state_backed_batch(state)
  released: list[str] = []
  batch.release_member = lambda member: released.append(member.claim_name)
  cluster = MagicMock()
  for member in state.members("pool-0"):
    BatchClaim(batch=batch, member=member, expires_at=1e12, cluster=cluster).release()
  assert len(released) == 10
  assert cluster.mock_calls == [], f"BatchClaim.release sent requests: {cluster.mock_calls[:4]}"


def test_batch_claims_acquire_is_o1_and_sends_no_requests() -> None:
  # BatchClaims.acquire runs once per episode in claim_mode="batch". With the cluster's
  # batch open, it must cost the same at any pool size and send nothing besides the
  # batch's own acquire.
  sizes = (50, 2000)

  def build(n: int) -> Callable[[], object]:
    state = _acquired_state(n)
    batch = _state_backed_batch(state)

    def acquire(pool: str, timeout: float | None = None, *,
                pod_annotations: dict[str, str] | None = None) -> Any:
      name = state.start_acquire(pool)
      state.upsert_claim({
          "metadata": {"name": name, "uid": f"uid-{name}", "resourceVersion": "1"},
          "spec": {"warmPoolRef": {"name": pool}},
          "status": {
              "sandbox": {"name": f"sbx-{name}", "podIPs": ["10.2.0.1"]},
              "conditions": [{"type": "Ready", "status": "True",
                              "reason": "SandboxReady"}],
          },
      })
      return state.acquired_member(name)

    batch.acquire = acquire
    claims = BatchClaims(BatchClaimConfig(), ready_timeout=60, labels={})
    cluster = MagicMock()
    cluster.name = "solo"
    claims_any: Any = claims
    claims_any._live[cluster.name] = batch

    def run() -> None:
      claims.acquire(cluster, "pool-0")
      assert cluster.mock_calls == [], "BatchClaims.acquire sent Kubernetes requests"

    return run

  assert_o1("BatchClaims.acquire (batch open)", build, sizes)


def test_fleet_batch_mode_acquire_is_o1_in_pool_size(make_cluster: Any) -> None:
  # In claim_mode="batch", fleet.acquire() takes a member from the cluster's batch and
  # connects it every episode; that whole path must not grow with the pool.
  sizes = (50, 2000)

  def build(n: int) -> Callable[[], object]:
    cluster = make_cluster("solo")
    fleet = SandboxFleet(FleetConfig(claim_mode="batch", install_teardown_hooks=False),
                         registry=ClusterRegistry([cluster]))
    pool = fleet.config.pool_name("img")
    state = _acquired_state(n, pool)
    batch = _state_backed_batch(state)

    def acquire(warmpool: str, timeout: float | None = None, *,
                pod_annotations: dict[str, str] | None = None) -> Any:
      name = state.start_acquire(warmpool)
      state.upsert_claim({
          "metadata": {"name": name, "uid": f"uid-{name}", "resourceVersion": "1"},
          "spec": {"warmPoolRef": {"name": warmpool}},
          "status": {
              "sandbox": {"name": f"sbx-{name}", "podIPs": ["10.3.0.1"]},
              "conditions": [{"type": "Ready", "status": "True",
                              "reason": "SandboxReady"}],
          },
      })
      return state.acquired_member(name)

    def connect(member: Any) -> Any:
      sandbox = MagicMock()
      sandbox.sandbox_id = member.sandbox_name
      return sandbox

    batch.acquire = acquire
    batch.connect = connect
    batch.release_member = lambda member: state.release_claim(member.claim_name, pool)
    cluster.sandbox_client.claim_batch.side_effect = lambda *_a, **_k: batch
    _route_pod_reads(cluster)
    # Open the batch and create the on-demand pool once, so the measured acquire takes
    # the steady-state path.
    fleet.release(fleet.acquire(Task(id="warm", image="img")))
    task = Task(id="measured", image="img")
    sent = len(_cluster_requests(cluster))

    def run() -> None:
      fleet.acquire(task)
      # The batch's watch already knows the member's Sandbox and pod, so no pod GET either.
      assert _cluster_requests(cluster)[sent:] == [], (
          f"a batch-mode acquire sent {_cluster_requests(cluster)[sent:]}")

    return run

  assert_o1("SandboxFleet.acquire (claim_mode=batch)", build, sizes)


def test_planned_fleet_acquire_is_o1_in_plan_size(make_cluster: Any) -> None:
  # After plan(), every acquire looks its image up in the plan; that must not grow with
  # the number of planned images (one per SWE-bench repo, thousands per run).
  sizes = (10, 2000)

  def build(i: int) -> Callable[[], object]:
    cluster = make_cluster("solo")
    fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False, max_concurrent=4),
                         registry=ClusterRegistry([cluster]))
    _route_pod_reads(cluster)
    fleet.load_tasks([f"img-{k}" for k in range(i)])
    plan = fleet.plan()
    # The last planned image, so a lookup that scans the plan walks all of it.
    image = plan.entries[-1].image
    fleet.release(fleet.acquire(Task(id="warm", image=image)))
    _count_walks(fleet, plan, at_least=i)
    task = Task(id="measured", image=image)
    return lambda: fleet.acquire(task)

  assert_o1("SandboxFleet.acquire (planned image)", build, sizes)


def test_exec_clients_are_cached_per_thread() -> None:
  # Each exec on a thread reuses that thread's CoreV1Api; building a client per exec
  # leaks a urllib3 pool and its threads every step.
  from agent_sandbox_rl.cluster import Cluster
  from agent_sandbox_rl.config import ClusterConfig

  api_client = MagicMock()
  api_client.configuration.connection_pool_maxsize = 1000
  cluster = Cluster(ClusterConfig(name="c1", namespace="ns1"), api_client=api_client)
  with patch("agent_sandbox_rl.cluster.client.ApiClient") as built, \
      patch("agent_sandbox_rl.cluster.client.CoreV1Api"):
    exec_core_api: Callable[[], object] = cluster.exec_core_api
    for _ in range(100):
      exec_core_api()
  assert built.call_count == 1, f"{built.call_count} ApiClients for 100 execs on one thread"


def test_on_demand_acquire_is_o1_in_known_images(make_cluster: Any) -> None:
  # The fleet remembers every on-demand (cluster, image) it created a pool for; checking
  # that memory per acquire must not grow with the number of images seen.
  sizes = (50, 2000)

  def build(k: int) -> Callable[[], object]:
    cluster = make_cluster("solo")
    fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False),
                         registry=ClusterRegistry([cluster]))
    _route_pod_reads(cluster)
    fleet.release(fleet.acquire(Task(id="warm", image="img")))
    fleet_any: Any = fleet
    for j in range(k):
      key = ("solo", f"seen-{j}")
      if isinstance(fleet_any._ondemand, set):
        fleet_any._ondemand.add(key)
      else:
        fleet_any._ondemand.append(key)
    _count_walks(fleet, at_least=k)
    task = Task(id="measured", image="img")
    return lambda: fleet.acquire(task)

  assert_o1("SandboxFleet.acquire (on-demand, known image)", build, sizes)


def _fleet_with_handles(make_cluster: Any,
                        h: int) -> tuple[SandboxFleet, list[SandboxHandle]]:
  cluster = make_cluster("solo")
  _route_pod_reads(cluster)
  fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False),
                       registry=ClusterRegistry([cluster]))
  handles = [
      SandboxHandle(
          task=Task(id=f"t-{i}", image="img"), cluster_name="solo",
          claim_name=f"c-{i}", sandbox_id=f"s-{i}", pod_name=f"p-{i}",
          hostname=f"s-{i}", _cluster=cluster,
          _batch_claim=cast(Any, types.SimpleNamespace(release=lambda: None)))
      for i in range(h)
  ]
  fleet_any: Any = fleet
  # Seed whichever container the fleet uses: a list, or an id-keyed dict.
  if isinstance(fleet_any._handles, dict):
    fleet_any._handles = {id(handle): handle for handle in handles}
  else:
    fleet_any._handles = list(handles)
  cluster.active_claims = h
  fleet_any._claims_reserved = h
  _count_walks(fleet, at_least=h)
  return fleet, handles


def test_fleet_release_is_o1_in_active_handles(make_cluster: Any) -> None:
  # F-33: release() runs `handle not in self._handles` and
  # `self._handles.remove(handle)` under fleet._lock, and each comparison runs the
  # generated dataclass __eq__ of SandboxHandle (including Task.__eq__).
  sizes = (50, 1000)

  def build(h: int) -> Callable[[], object]:
    fleet, handles = _fleet_with_handles(make_cluster, h)
    target = handles[-1]

    fleet_any: Any = fleet

    def run() -> None:
      fleet.release(target)
      # A release that found nothing to remove would measure a no-op (len() is O(1), unlike
      # handles(), which copies the list).
      assert len(fleet_any._handles) == h - 1

    return run

  assert_o1("SandboxFleet.release(last handle)", build, sizes)


def test_fleet_acquire_is_o1_in_active_handles(make_cluster: Any) -> None:
  # acquire() registers the handle under fleet._lock, so any scan of the live handles
  # there costs O(H) per acquire and O(H^2) for a fleet ramping up to H sandboxes.
  sizes = (50, 1000)

  def build(h: int) -> Callable[[], object]:
    fleet, _handles = _fleet_with_handles(make_cluster, h)
    # Create the on-demand pool once, so the measured acquire takes the steady-state path.
    fleet.release(fleet.acquire(Task(id="warm", image="img")))
    task = Task(id="measured", image="img")
    # measure() runs each fresh build once, so no sample needs to undo the acquire.
    return lambda: fleet.acquire(task)

  assert_o1("SandboxFleet.acquire", build, sizes)


def test_repeat_on_demand_acquires_skip_pool_and_template_requests(
    make_cluster: Any) -> None:
  # F-34 (PERF-23): the size-1 on-demand pool for (cluster, image) is created once;
  # repeat acquires reuse it. Each repeat acquire may send only its own claim and, in
  # per_claim mode, the one pod-identity GET (record_pod_identity), plus the reads the
  # returned Sandbox makes: get_pod_name() and get_pod_ip() each GET the Sandbox today
  # (F-38), so each is allowed once; the bound still passes once F-38 drops one.
  cluster = make_cluster("solo")
  fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False),
                       registry=ClusterRegistry([cluster]))
  acquires = 10
  _route_pod_reads(cluster)
  tasks = fleet.load_tasks(["img"] * acquires)
  fleet.release(fleet.acquire(tasks[0]))
  sandboxes: list[MagicMock] = []
  make_sandbox = cluster.sandbox_client.create_sandbox.side_effect

  def recorded_sandbox(*args: Any, **kwargs: Any) -> MagicMock:
    sandbox = cast(MagicMock, make_sandbox(*args, **kwargs))
    sandboxes.append(sandbox)
    return sandbox

  cluster.sandbox_client.create_sandbox.side_effect = recorded_sandbox
  client_first = len(cluster.sandbox_client.mock_calls)
  ensure = cluster.resources.ensure_template.call_count
  create = cluster.resources.create_warmpool.call_count
  assert (ensure, create) == (1, 1), (
      f"{ensure} ensure_template and {create} create_warmpool calls for the first acquire")

  first = len(_cluster_requests(cluster))
  for task in tasks[1:]:
    fleet.release(fleet.acquire(task))
  repeat = _cluster_requests(cluster)[first:]
  extra = [name for name in repeat if name != "read_namespaced_pod_status"]
  assert extra == [] and len(repeat) <= acquires - 1, (
      f"{len(repeat)} Kubernetes requests across {acquires - 1} repeat acquires of one "
      f"image (expected at most one pod-identity GET each): {repeat[:4]}")
  assert cluster.sandbox_client.create_sandbox.call_count == acquires
  # mock_calls also holds calls through the client's attributes (its K8s helper).
  client_calls = [name for name, _a, _k in cluster.sandbox_client.mock_calls[client_first:]]
  assert client_calls == ["create_sandbox"] * (acquires - 1), (
      f"repeat acquires sent SDK client calls besides their creates: {client_calls[:6]}")
  allowed = {"get_pod_name", "get_pod_ip", "terminate"}
  assert len(sandboxes) == acquires - 1
  for sandbox in sandboxes:
    calls = [name for name, _a, _k in sandbox.mock_calls]
    assert set(calls) <= allowed and len(calls) == len(set(calls)), (
        f"one acquire and release made Sandbox calls beyond one pod-name read, one pod-IP "
        f"read, and the terminate: {calls}")


def test_per_step_exec_and_liveness_send_one_request_each(make_cluster: Any) -> None:
  # Every RL step execs one command and checks is_alive(): one exec stream and one pod GET
  # per step, in both claim modes (batch mode skips the GET only once the batch watch has
  # reported the claim lost, which test_handle_is_alive_on_lost_claim_* covers).
  from unittest.mock import patch as patch_
  cluster = make_cluster("solo")
  _route_pod_reads(cluster)
  cluster.core_api.read_namespaced_pod_status.return_value = {
      "metadata": {"uid": "u"}, "status": {"phase": "Running"}}
  state = _acquired_state(1)
  member = state.members("pool-0")[0]
  assert member.sandbox_name is not None
  per_claim = SandboxHandle(
      task=Task(id="t", image="img"), cluster_name="solo", claim_name="c", sandbox_id="s",
      pod_name="p", hostname="s", _cluster=cluster)
  batch_mode = SandboxHandle(
      task=Task(id="t", image="img"), cluster_name="solo", claim_name=member.claim_name,
      sandbox_id=member.sandbox_name, pod_name=member.sandbox_name,
      hostname=member.sandbox_name, _cluster=cluster,
      _batch_claim=BatchClaim(batch=_state_backed_batch(state), member=member,
                              expires_at=1e12, cluster=cluster))
  steps = 10
  for handle, pod_gets in ((per_claim, steps), (batch_mode, steps)):
    sent = len(_cluster_requests(cluster))
    with patch_("agent_sandbox_rl.handles.stream", return_value="ok") as streams:
      for _ in range(steps):
        handle.exec("true")
        assert handle.is_alive()
    assert streams.call_count == steps, f"{streams.call_count} exec streams for {steps} steps"
    requests = _cluster_requests(cluster)[sent:]
    assert requests == ["read_namespaced_pod_status"] * pod_gets, (
        f"{steps} steps sent {requests[:6]} (expected {pod_gets} pod GETs)")


@pytest.mark.xfail(strict=True, reason="F-38: a per_claim acquire reads the Sandbox twice "
                   "(get_pod_name and get_pod_ip); this turns green, and must be un-marked, "
                   "once one read serves both")
def test_per_claim_acquire_reads_the_sandbox_once(make_cluster: Any) -> None:
  # With a real SDK Sandbox (its K8s helper counting reads), one acquire needs the Sandbox's
  # pod name and pod IP, which one GET of the Sandbox returns together.
  from k8s_agent_sandbox.sandbox import Sandbox
  cluster = make_cluster("solo")
  _route_pod_reads(cluster)
  fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False),
                       registry=ClusterRegistry([cluster]))
  fleet.release(fleet.acquire(Task(id="warm", image="img")))
  reads = [0]

  def get_sandbox(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    reads[0] += 1
    return {"metadata": {"annotations": {}}, "status": {"podIPs": ["10.6.0.1"]}}

  def real_sandbox(**kwargs: Any) -> Sandbox:
    helper = MagicMock()
    helper.get_sandbox.side_effect = get_sandbox
    sandbox = Sandbox(claim_name=kwargs.get("claim_name") or "c", sandbox_id="sb",
                      namespace="ns", k8s_helper=helper)
    sandbox_any: Any = sandbox
    sandbox_any.terminate = lambda: None
    return sandbox

  cluster.sandbox_client.create_sandbox.side_effect = real_sandbox
  acquires = 5
  for i in range(acquires):
    fleet.release(fleet.acquire(Task(id=f"t-{i}", image="img")))
  assert reads[0] <= acquires, f"{reads[0]} Sandbox GETs across {acquires} acquires"


class _ChunkedResp:
  """A session websocket that delivers ``chunks`` stdout chunks of ``size`` bytes, then the
  command's end marker."""

  def __init__(self, chunks: int, size: int, marker: str) -> None:
    self.pending = ["x" * size] * chunks + [marker + "\r\n"]

  def write_stdin(self, _data: str) -> None:
    pass

  def update(self, timeout: float) -> None:
    pass

  def peek_stderr(self) -> bool:
    return False

  def peek_stdout(self) -> bool:
    return bool(self.pending)

  def read_stdout(self) -> str:
    return self.pending.pop(0)

  def is_open(self) -> bool:
    return True


SESSION_CHUNKS = (50, 2000)
SESSION_CHUNK_BYTES = 1024


def test_session_run_cost_is_linear_in_output_size() -> None:
  # F-39: SandboxSession.run (every command of a recycled sandbox with use_session=True)
  # must cost time linear in the command's output. The quadratic version re-joins and
  # re-scans the whole buffer on every chunk, which is C work: it executes no extra
  # bytecode and its peak memory stays linear, so only time shows it. The sizes differ
  # 40x; linear is about 40x and quadratic about 1,600x, so the 160x bound leaves 4x of
  # room for timer noise either way. Check 8's INV-PERF-HOTPATH-5 flags the same pattern
  # statically.
  def run_once(chunks: int) -> float:
    session = object.__new__(SandboxSession)
    session_any: Any = session
    session_any.pod, session_any.container, session_any._seq = "p", None, 0
    session_any._resp = _ChunkedResp(chunks, SESSION_CHUNK_BYTES, "ASRLDONE1X")
    started = time.perf_counter()
    out = session.run("true")
    elapsed = time.perf_counter() - started
    assert len(out) == chunks * SESSION_CHUNK_BYTES
    return elapsed

  small, large = (min(run_once(k) for _ in range(5)) for k in SESSION_CHUNKS)
  size_ratio = SESSION_CHUNKS[1] / SESSION_CHUNKS[0]
  assert large / small <= 4 * size_ratio, (
      f"SandboxSession.run: {small * 1e3:.2f} ms for {SESSION_CHUNKS[0]} chunks vs "
      f"{large * 1e3:.2f} ms for {SESSION_CHUNKS[1]} ({large / small:.0f}x for "
      f"{size_ratio:.0f}x the output)")


@pytest.mark.parametrize("backing", BACKINGS)
def test_batch_watchdog_idle_poll_is_o1_in_batch_size(backing: str) -> None:
  # 26/N (failfast.py) only. With no pending member, _poll_once must not copy and sort
  # every member (members()) or rebuild O(N) sets under the batch lock, since it polls at
  # up to 50 Hz during acquire bursts. It does both on 26/N, so this gate is red there
  # until the watchdog reads only the pending members it acts on.
  failfast = pytest.importorskip("agent_sandbox_rl.failfast")
  watchdog_cls = getattr(failfast, "BatchWatchdog", None)
  if watchdog_cls is None:
    # BatchWatchdog arrives with 26/N (feat/rl-batch-hardening); 25/N has only the
    # per-claim ClaimWatchdog, which never touches a batch.
    pytest.skip("BatchWatchdog is not on this branch (added in 26/N)")
  sizes = (50, 2000)

  def build(n: int) -> Callable[[], object]:
    batch, _state, requests = _backed_batch(n, backing)
    watchdog = watchdog_cls(
        MagicMock(), batch,
        types.SimpleNamespace(poll_s=1.0, initial_delay_s=5.0), lambda *_a: None)

    def run() -> None:
      watchdog._poll_once()
      assert requests == [], "an idle watchdog poll sent Kubernetes requests"

    return run

  assert_o1(f"BatchWatchdog._poll_once (0 pending, {backing})", build, sizes)


_EPISODE_CHURN_SCRIPT = """
import contextlib, gc, logging, sys, tracemalloc
sys.path.insert(0, sys.argv[4])
from conftest import FakeCluster
from k8s_agent_sandbox.batch_state import BatchState
from k8s_agent_sandbox.models import BatchGroup
from agent_sandbox_rl import BatchClaimConfig, ClusterRegistry, FleetConfig, SandboxFleet
from agent_sandbox_rl.batch_claims import BatchClaims
from agent_sandbox_rl.sources import Task

WARMUP, EPISODES = int(sys.argv[2]), int(sys.argv[3])
MODE, _, RECORDING = sys.argv[1].partition("+")
logging.disable(logging.WARNING)


class PlainPod:
  def to_dict(self):
    return {"metadata": {"uid": "pod-uid"},
            "status": {"phase": "Running", "containerStatuses": []}}


class PodReads:
  # Stands in for cluster.core_api and cluster.exec_core_api() (25/N reads the pod through
  # the latter, 26/N through the former): counts pod GETs without recording them.
  def __init__(self):
    self.count = 0

  def read_namespaced_pod_status(self, *_args, **_kwargs):
    self.count += 1
    return PlainPod()


def check_pod_reads(pods, before, episodes, allowed_per_episode):
  if pods.count - before > allowed_per_episode * episodes:
    sys.exit(f"{pods.count - before} pod GETs across {episodes} episodes "
             f"(at most {allowed_per_episode} each)")


def maybe_recording(fleet):
  return fleet.recording() if RECORDING else contextlib.nullcontext()


class PlainSandbox:
  def __init__(self, member):
    self.sandbox_id = member.sandbox_name
    self.claim_name = member.claim_name


class PlainBatch:
  # A real BatchState behind plain functions: a MagicMock batch would retain every call, and
  # the measured bytes would be the mock's rather than the fleet's.
  def __init__(self):
    self.batch_id = "bchurn"
    self.state = BatchState("bchurn", [BatchGroup(warmpool="pool-0", size=0)])
    self.rv = 0

  def err(self):
    return None

  def members(self, warmpool=None):
    return self.state.members(warmpool)

  def get_member(self, claim_name):
    return self.state.get_member(claim_name)

  def acquire(self, warmpool, timeout=None, *, pod_annotations=None):
    name = self.state.start_acquire(warmpool)
    self.rv += 1
    self.state.upsert_claim({
        "metadata": {"name": name, "uid": "uid-" + name, "resourceVersion": str(self.rv)},
        "spec": {"warmPoolRef": {"name": warmpool}},
        "status": {
            "sandbox": {"name": "sbx-" + name, "podIPs": ["10.4.0.1"]},
            "conditions": [{"type": "Ready", "status": "True", "reason": "SandboxReady"}],
        },
    })
    return self.state.acquired_member(name)

  def connect(self, member):
    return PlainSandbox(member)

  def release_member(self, member):
    # The SDK handle releases the claim, and the watch's DELETED event forgets it.
    self.state.release_claim(member.claim_name, member.warmpool)
    self.state.note_deleted(member.claim_name, self.state.delete_seq(member.claim_name))


def mock_calls(cluster):
  return [name for m in (cluster.resources, cluster.custom_api, cluster.core_api,
                         cluster.apps_api, cluster.api_client, cluster.sandbox_client)
          for name, _args, _kwargs in getattr(m, "mock_calls", [])]


def measure(run_episodes, batch, cluster):
  # Trace from before the warmup, so state the measured episodes replace is traced too.
  tracemalloc.start()
  run_episodes(WARMUP)
  # Both readings without the batch state's released-name tombstones (F-35, pinned in the
  # SDK suite), so only what the RL layer keeps is counted.
  batch.state._released.clear()
  gc.collect()
  before = tracemalloc.get_traced_memory()[0]
  calls_before = len(mock_calls(cluster))
  run_episodes(EPISODES)
  extra = mock_calls(cluster)[calls_before:]
  if extra:
    sys.exit(f"episodes sent {len(extra)} Kubernetes or SDK client calls besides the "
             f"batch's, e.g. {extra[:3]}")
  batch.state._released.clear()
  gc.collect()
  print((tracemalloc.get_traced_memory()[0] - before) / EPISODES)


def claims_main():
  batch = PlainBatch()
  cluster = FakeCluster("solo")
  claims = BatchClaims(BatchClaimConfig(), ready_timeout=60, labels={})
  claims._live[cluster.name] = batch

  def run(count):
    for _ in range(count):
      claims.acquire(cluster, "pool-0").release()

  measure(run, batch, cluster)


def fleet_main():
  batch = PlainBatch()
  cluster = FakeCluster("solo")
  fleet = SandboxFleet(FleetConfig(claim_mode="batch", install_teardown_hooks=False),
                       registry=ClusterRegistry([cluster]))
  pool = fleet.config.pool_name("img")
  batch.state = BatchState("bchurn", [BatchGroup(warmpool=pool, size=0)])
  cluster.sandbox_client.claim_batch.side_effect = lambda *_a, **_k: batch
  pods = PodReads()
  cluster.core_api = pods
  cluster.exec_core_api = lambda: pods
  task = Task(id="churn", image="img")
  stack = contextlib.ExitStack()
  stack.enter_context(maybe_recording(fleet))
  # Open the batch and create the on-demand pool once, outside the measurement.
  fleet.release(fleet.acquire(task))
  # 26/N's BatchWatchdog forgets handed-out names on its next poll, which its thread runs on
  # a timer. Stop the thread and run that poll before each reading, so the readings don't
  # depend on when it last ran; a name that survives the poll is still counted.
  watchdogs = list(getattr(fleet._batch_claims, "_watchdogs", {}).values())
  for watchdog in watchdogs:
    watchdog.stop()
    watchdog.join(10)

  def run(count):
    before = pods.count
    for _ in range(count):
      fleet.release(fleet.acquire(task))
    for watchdog in watchdogs:
      watchdog._poll_once()
    # Batch mode knows each member's pod from the batch watch.
    check_pod_reads(pods, before, count, 0)

  measure(run, batch, cluster)
  stack.close()


class PlainClaimSandbox:
  def __init__(self, claim_name, seq):
    self.claim_name = claim_name
    self.sandbox_id = "sb-" + str(seq)
    self.pod = "pod-" + str(seq)

  def get_pod_name(self):
    return self.pod

  def get_pod_ip(self):
    return "10.5.0.1"

  def terminate(self):
    pass


class PlainSandboxClient:
  # Tracks claims the way SandboxClient.create_sandbox does, so a fleet that never forgets a
  # released claim in the client (forget_claim_in_client) shows up as retained memory.
  def __init__(self, namespace):
    self.namespace = namespace
    self.seq = 0
    self._active_connection_sandboxes = {}
    self._explicit_claims = set()

  def create_sandbox(self, warmpool=None, namespace=None, claim_name=None, **_kwargs):
    self.seq += 1
    name = claim_name or "claim-" + str(self.seq)
    sandbox = PlainClaimSandbox(name, self.seq)
    self._active_connection_sandboxes[(namespace, name)] = sandbox
    if claim_name:
      self._explicit_claims.add((namespace, name))
    return sandbox

  def delete_sandbox(self, claim_name, namespace=None):
    self._active_connection_sandboxes.pop((namespace, claim_name), None)
    self._explicit_claims.discard((namespace, claim_name))


class PlainState:
  # measure() clears BatchState tombstones; per-claim mode has no batch.
  _released = set()


def per_claim_main():
  cluster = FakeCluster("solo")
  plain_client = PlainSandboxClient(cluster.namespace)
  fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False),
                       registry=ClusterRegistry([cluster]))
  pods = PodReads()
  cluster.core_api = pods
  cluster.exec_core_api = lambda: pods
  task = Task(id="churn", image="img")
  stack = contextlib.ExitStack()
  stack.enter_context(maybe_recording(fleet))
  fleet.release(fleet.acquire(task))
  # Swapped in after the first acquire, so the mock's record of it stays out of the count.
  cluster.sandbox_client = plain_client
  holder = type("Holder", (), {"state": PlainState()})()

  def run(count):
    before = pods.count
    for _ in range(count):
      fleet.release(fleet.acquire(task))
    # The one pod-identity GET per acquire (record_pod_identity).
    check_pod_reads(pods, before, count, 1)

  measure(run, holder, cluster)
  stack.close()


{"claims": claims_main, "fleet": fleet_main, "per_claim": per_claim_main}[MODE]()
"""

CHURN_WARMUP = 300
CHURN_EPISODES = 3000


def _retained_per_episode(mode: str) -> float:
  """Runs ``CHURN_EPISODES`` batch-mode episodes in a fresh interpreter (a test runner's own
  bookkeeping would otherwise be counted) and returns the bytes retained per episode."""
  tests_dir = pathlib.Path(__file__).resolve().parent
  env = dict(os.environ)
  env["PYTHONPATH"] = os.pathsep.join(
      [str(tests_dir.parent), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])])
  proc = subprocess.run(
      [sys.executable, "-c", _EPISODE_CHURN_SCRIPT, mode, str(CHURN_WARMUP),
       str(CHURN_EPISODES), str(tests_dir)],
      capture_output=True, text=True, env=env, timeout=300, check=False)
  assert proc.returncode == 0, proc.stderr[-2000:]
  return float(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "mode", ["claims", "fleet", "fleet+recording", "per_claim", "per_claim+recording"])
def test_fleet_episodes_retain_no_memory(mode: str) -> None:
  # Every RL episode acquires and releases one sandbox (a batch member in claim_mode="batch",
  # a claim of its own in per_claim mode); a long-lived fleet runs millions, so anything kept
  # per episode (a handed-out BatchClaim, a handle, a client-side claim record, a run-report
  # sample, a log record) grows without bound. fleet.run() always records a RunReport, so the
  # "+recording" modes run the episodes inside fleet.recording(). One reference is 8 B.
  per_episode = _retained_per_episode(mode)
  assert per_episode < 4, f"{per_episode:.1f} B/episode retained ({mode})"
