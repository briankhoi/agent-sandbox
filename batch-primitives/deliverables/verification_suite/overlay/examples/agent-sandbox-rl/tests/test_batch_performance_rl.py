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
liveness on a lost claim, the BatchClaim and batch-mode acquire paths, and the idle
watchdog send no Kubernetes request, pins repeat on-demand acquires to their minimum number of
requests (pod reads included),
bounds the memory a long-lived fleet retains per episode (each episode run through
``process_parallel``, the async fleet's executor, or the recycle executor) with run recording
off and on, requires session command output and every executor to cost time linear in their
size (with claims at one per task, or one per image per ``max_reuses`` tasks when recycling),
holds the lookahead warmer to one pool request per changed image with no table or memory
growth over the stream, gates the real pool operations and git-restore reset the executors
stub out (constant cost in plan size, one exec per reset), pins background polls to their
configured intervals, requires per-episode requests to run with the fleet's locks free, and
requires steady-state paths to log nothing at INFO or above. See the SDK counterpart
``k8s_agent_sandbox/test/unit/test_batch_performance_invariants.py`` for the counting method
and the scope (cost growth, not constant factors).

Behavior already pinned elsewhere is not repeated here: ``test_batch_claims.py`` covers the
zero-GET ``is_alive()`` on claim loss (WATCH-10) and the single ``size=0`` seed group on the
first acquire (PERF-20).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import gc
import itertools
import logging
import os
import time
import pathlib
import subprocess
import sys
import threading
import tracemalloc
import types
from collections.abc import Callable, Sized
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

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


# A cold per_claim acquire starts its fail-fast ClaimWatchdog thread by design; that thread's
# per-poll requests and its poll cadence have gates of their own.
_EXPECTED_THREADS = ("asrl-failfast-",)


class _HandOffs:
  """Records work a measured call hands to another thread (a thread it starts, a callable it
  submits to an executor). No instruction count, peak, or walk count sees that work, so the
  measuring helpers fail on it instead of undercounting. Only ``_EXPECTED_THREADS`` pass."""

  def __enter__(self) -> "_HandOffs":
    self.handed_off: list[str] = []
    real_start, real_submit = threading.Thread.start, ThreadPoolExecutor.submit
    handed_off = self.handed_off

    def start(thread: threading.Thread) -> None:
      if not thread.name.startswith(_EXPECTED_THREADS):
        handed_off.append(f"started thread {thread.name}")
      real_start(thread)

    def submit(executor: ThreadPoolExecutor, fn: Callable[..., Any], /, *args: Any,
               **kwargs: Any) -> Any:
      handed_off.append(f"submitted {getattr(fn, '__qualname__', fn)} to an executor")
      return real_submit(executor, fn, *args, **kwargs)

    self._patches: list[Any] = [patch.object(threading.Thread, "start", start),
                                patch.object(ThreadPoolExecutor, "submit", submit)]
    for patcher in self._patches:
      patcher.start()
    return self

  def __exit__(self, exc_type: object, *_exc: object) -> None:
    for patcher in reversed(self._patches):
      patcher.stop()
    if self.handed_off and exc_type is None:
      raise AssertionError(
          f"the measured call handed work off where no count sees it: {self.handed_off[:3]}")


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
        with _HandOffs():
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
    with _HandOffs():
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


# Only operations whose cost grows with the container itself (`s.update(x)` walks x). The
# same lists as the SDK suite's _count_walks_through.
_ORDER = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__")
_DICT_WALKS = ("__iter__", "keys", "values", "items", "copy", "__or__", "__ror__",
               "__reversed__", "__eq__", "__ne__")
_COUNTING: dict[type, type] = {
    dict: _counting(dict, _DICT_WALKS),
    set: _counting(set, ("__iter__", "copy", "issubset", "__le__", "__lt__", "__eq__", "__ne__",
                         "union", "__or__", "__ror__", "difference", "__sub__",
                         "symmetric_difference", "__xor__", "__rxor__", "intersection_update",
                         "__iand__")),
    list: _counting(list, ("__iter__", "copy", "__contains__", "index", "count", "remove",
                           "__reversed__", "sort", "reverse", "__add__", "__mul__", *_ORDER)),
    collections.deque: _counting(collections.deque, (
        "__iter__", "copy", "__contains__", "index", "count", "remove", "__reversed__",
        "reverse", *_ORDER)),
}


# Walks of a dict subclass (Counter, defaultdict, OrderedDict) beyond the plain dict's.
_DICT_SUBCLASS_WALKS = ("most_common", "elements", "total", "__add__", "__sub__", "__and__")


def _counting_copy(value: Any) -> Any:
  """A walk-counting copy of ``value`` (``None`` for a type with no counting form), building
  a counting subclass once per dict subclass such as ``Counter`` or ``defaultdict``."""
  base = type(value)
  if base in _COUNTING.values():
    return None                                 # already counting
  counting = _COUNTING.get(base)
  if counting is None and isinstance(value, dict):
    names = (*_DICT_WALKS, *_DICT_SUBCLASS_WALKS)
    counting = _COUNTING[base] = _counting(base, tuple(n for n in names if hasattr(base, n)))
  if counting is None:
    return None
  if isinstance(value, collections.defaultdict):
    return counting(value.default_factory, value)
  return counting(value)


def _count_walks(*owners: Any, at_least: int) -> None:
  """Swaps every dict (subclasses included), set, list, or deque attribute of ``owners``
  holding at least ``at_least`` entries for a walk-counting copy."""
  for owner in owners:
    for attr, value in list(vars(owner).items()):
      if isinstance(value, dict):
        # Per-pool indexes (`_members_by_pool[pool]`) hold one table per pool.
        for key, inner in list(value.items()):
          if isinstance(inner, Sized) and len(inner) >= at_least:
            inner_copy = _counting_copy(inner)
            if inner_copy is not None:
              value[key] = inner_copy
      if not isinstance(value, Sized) or len(value) < at_least:
        continue
      copy = _counting_copy(value)
      if copy is None:
        continue
      if isinstance(copy, list):
        # The copy is sized exactly, unlike a list grown by appends; without spare
        # capacity the next append's realloc would read as a whole-table copy.
        list.append(copy, None)
        list.pop(copy)
      setattr(owner, attr, copy)


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
      with _HandOffs():
        fn()
      peaks.append(tracemalloc.get_traced_memory()[1] - base)
    finally:
      if started:
        tracemalloc.stop()
  return min(peaks)


def _assert_quiet(label: str, run: Callable[[], object]) -> None:
  """Fails if ``run`` logs at INFO or above from the SDK or the RL package: on a per-episode
  path that is a driver log line per claim (PERF-9). The loggers are lowered to INFO for the
  run, so records the default WARNING level would drop are still seen."""
  records: list[logging.LogRecord] = []

  class Recorder(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
      records.append(record)

  handler = Recorder()
  loggers = [logging.getLogger(name) for name in ("agent_sandbox_rl", "k8s_agent_sandbox")]
  levels = [logger.level for logger in loggers]
  for logger in loggers:
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
  try:
    run()
  finally:
    for logger, level in zip(loggers, levels):
      logger.removeHandler(handler)
      logger.setLevel(level)
  loud = [f"{r.levelname} {r.name}: {r.getMessage()}" for r in records]
  assert loud == [], f"{label} logged at INFO or above on a steady-state path: {loud[:3]}"


def assert_o1(label: str, build: Callable[[int], Callable[[], object]],
              sizes: tuple[int, int], *, quiet: bool = True) -> None:
  """Asserts flat instruction counts, flat peak allocation, and no whole-table walks between
  the two sizes, and (unless ``quiet`` is False) that one more run logs nothing at INFO or
  above."""
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
  if quiet:
    _assert_quiet(label, build(sizes[0]))


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


def _assert_unlocked(label: str, *locks: Any) -> None:
  """Fails if the calling thread (or any other) holds one of ``locks``: a lock held across a
  Kubernetes request stalls every worker that needs it for a round trip."""
  for lock in locks:
    assert lock.acquire(timeout=1), f"{label} ran while holding {lock!r}"
    lock.release()


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
      # The batch's acquire may wait out a claim create, so no fleet lock may be held.
      fleet_any: Any = fleet
      _assert_unlocked("the batch-mode acquire", fleet_any._lock, fleet_any._batch_claims._lock)
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


@pytest.mark.parametrize("image", ["planned", "fresh"])
def test_add_tasks_is_o1_in_plan_size(make_cluster: Any, image: str) -> None:
  # F-40: add_tasks streams each training batch's tasks into a planned fleet (the lookahead
  # warmer calls it for every batch with a new image), so it must not re-sum every planned
  # image's tasks per call. Copying fleet.tasks is a deliberate copy-on-write snapshot:
  # load_tasks hands out the list itself and the strategies index results by it, so the
  # gate holds the task count fixed and scales only the plan.
  sizes = (10, 2000)
  fresh = itertools.count()

  def build(i: int) -> Callable[[], object]:
    cluster = make_cluster("solo")
    fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False, max_concurrent=4),
                         registry=ClusterRegistry([cluster]))
    fleet.load_tasks([f"img-{k}" for k in range(i)])
    plan = fleet.plan()
    fleet.tasks = []
    _count_walks(fleet, plan, at_least=i)

    def run() -> None:
      name = plan.entries[-1].image if image == "planned" else f"new-{next(fresh)}"
      fleet.add_tasks([Task(id=f"{name}-{k}", image=name) for k in range(8)])

    return run

  # add_tasks logs one INFO line per call by design: one per streamed training batch, not
  # per episode.
  assert_o1(f"SandboxFleet.add_tasks ({image} image)", build, sizes, quiet=False)


def test_fleet_pool_ops_are_o1_in_plan_size(make_cluster: Any) -> None:
  # The executors and the lookahead warmer stub these out, so they are gated here on a real
  # planned fleet: set_pool_replicas runs once per held recycle claim, warm_image and
  # unwarm_image once per changed image per training batch. Each looks its image up in the
  # plan (one per SWE-bench repo, thousands per run) and sends the same pool requests at any
  # plan size.
  sizes = (10, 2000)
  sent: dict[int, list[str]] = {}

  def build(i: int) -> Callable[[], object]:
    cluster = make_cluster("solo")
    fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False, max_concurrent=4),
                         registry=ClusterRegistry([cluster]))
    fleet.load_tasks([f"img-{k}" for k in range(i)])
    plan = fleet.plan()
    image = plan.entries[-1].image
    fleet.warm_image(image, wait=False)
    _count_walks(fleet, plan, at_least=i)

    def run() -> None:
      before = len(_cluster_requests(cluster))
      fleet.set_pool_replicas(image, 3)
      fleet.set_pool_replicas(image, 1)
      fleet.unwarm_image(image)
      fleet.warm_image(image, wait=False)
      sent.setdefault(i, _cluster_requests(cluster)[before:])

    return run

  # Pool ops log one INFO line each by design: once per pool change, not per episode.
  assert_o1("SandboxFleet set_pool_replicas, unwarm_image, warm_image", build, sizes,
            quiet=False)
  assert sent[sizes[0]] == sent[sizes[1]], (
      f"pool ops sent {sent[sizes[0]]} at {sizes[0]} images vs {sent[sizes[1]]} at {sizes[1]}")


def test_git_restore_reset_sends_one_exec_each() -> None:
  # The recycle executors prime each claimed sandbox once and reset it between every two of
  # its tasks; each must be a single exec (one round trip through the held session), so a
  # recycled task costs one request beyond its own work.
  from agent_sandbox_rl.recycle import GitRestoreReset
  execs: list[object] = []
  output = "SHA=abc\nDIRTY=0\nPROCS=1\n"

  def exec_(command: object, **_kwargs: object) -> str:
    execs.append(command)
    return output

  handle = types.SimpleNamespace(exec=exec_, runtime_container=None, pod_name="p")
  reset = GitRestoreReset()
  baseline = reset.prime(cast(Any, handle))
  assert len(execs) == 1, f"prime sent {len(execs)} execs"
  for k in range(1, 6):
    reset.reset(cast(Any, handle), baseline, timeout=5.0)
    assert len(execs) == 1 + k, f"{k} resets sent {len(execs) - 1} execs"


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


def _refuse(*_args: Any, **_kwargs: Any) -> None:
  raise ApiException(status=403, reason="Forbidden")


def _fleet_with_handles(make_cluster: Any, h: int, mode: str = "batch",
                        refuse: bool = False) -> tuple[SandboxFleet, list[SandboxHandle]]:
  """A fleet with ``h`` live handles: batch-mode handles (released through their batch
  claim), or per_claim handles (each terminates its own Sandbox, then forgets its claim in
  the SDK client, whose registries hold all ``h``). With ``refuse``, each handle's delete is
  refused (403)."""
  release_claim = _refuse if refuse else (lambda: None)
  cluster = make_cluster("solo")
  _route_pod_reads(cluster)
  fleet = SandboxFleet(FleetConfig(install_teardown_hooks=False),
                       registry=ClusterRegistry([cluster]))
  per_claim = mode == "per_claim"
  if per_claim:
    keys = [(cluster.namespace, f"c-{i}") for i in range(h)]
    cluster.sandbox_client = types.SimpleNamespace(
        _active_connection_sandboxes={key: object() for key in keys},
        _explicit_claims=set(keys))
    _count_walks(cluster.sandbox_client, at_least=h)
  handles = [
      SandboxHandle(
          task=Task(id=f"t-{i}", image="img"), cluster_name="solo",
          claim_name=f"c-{i}", sandbox_id=f"s-{i}", pod_name=f"p-{i}",
          hostname=f"s-{i}", _cluster=cluster,
          sandbox=cast(Any, types.SimpleNamespace(terminate=release_claim)) if per_claim else None,
          _batch_claim=None if per_claim else cast(
              Any, types.SimpleNamespace(release=release_claim)))
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


@pytest.mark.parametrize("outcome", ["deleted", "refused"])
@pytest.mark.parametrize("mode", ["batch", "per_claim"])
def test_fleet_release_is_o1_in_active_handles(make_cluster: Any, mode: str,
                                               outcome: str) -> None:
  # F-33: release() runs `handle not in self._handles` and
  # `self._handles.remove(handle)` under fleet._lock, and each comparison runs the
  # generated dataclass __eq__ of SandboxHandle (including Task.__eq__). A per_claim
  # release also untracks its claim from the SDK client's registries (H entries). A refused
  # delete (an outage refuses every episode's) takes the same bookkeeping; it may log.
  sizes = (50, 1000)

  def build(h: int) -> Callable[[], object]:
    fleet, handles = _fleet_with_handles(make_cluster, h, mode, refuse=outcome == "refused")
    target = handles[-1]

    fleet_any: Any = fleet

    # A refused release keeps its handle, so the caller can retry it.
    left = h if outcome == "refused" else h - 1

    def run() -> None:
      with contextlib.suppress(ApiException):
        fleet.release(target)
      # A release that found nothing to remove would measure a no-op (len() is O(1), unlike
      # handles(), which copies the list).
      assert len(fleet_any._handles) == left

    return run

  assert_o1(f"SandboxFleet.release(last handle, {mode}, {outcome})", build, sizes,
            quiet=outcome == "deleted")


@pytest.mark.parametrize("outcome", ["created", "refused"])
def test_fleet_acquire_is_o1_in_active_handles(make_cluster: Any, outcome: str) -> None:
  # acquire() registers the handle under fleet._lock, so any scan of the live handles
  # there costs O(H) per acquire and O(H^2) for a fleet ramping up to H sandboxes. A refused
  # create (a quota or RBAC outage fails every episode's) unwinds its reservation, which must
  # not scan them either; it may log the failure.
  sizes = (50, 1000)

  def build(h: int) -> Callable[[], object]:
    fleet, _handles = _fleet_with_handles(make_cluster, h)
    # Create the on-demand pool once, so the measured acquire takes the steady-state path.
    fleet.release(fleet.acquire(Task(id="warm", image="img")))
    task = Task(id="measured", image="img")
    if outcome == "created":
      # measure() runs each fresh build once, so no sample needs to undo the acquire.
      return lambda: fleet.acquire(task)
    cluster = fleet.registry.get("solo")
    cluster.sandbox_client.create_sandbox.side_effect = ApiException(status=403)

    def refused() -> None:
      with pytest.raises(Exception):
        fleet.acquire(task)

    return refused

  assert_o1(f"SandboxFleet.acquire ({outcome})", build, sizes, quiet=outcome == "created")


def test_fleet_snapshot_ops_are_o1_in_active_handles(make_cluster: Any) -> None:
  # Snapshot workflows snapshot, suspend, and resume or restore a handle per episode. The
  # fleet's side of each (the phase, the claim-slot bookkeeping under fleet._lock) must not
  # scan the live handles; the snapshot work itself is the SDK's, stubbed here.
  sizes = (50, 1000)

  def build(h: int) -> Callable[[], object]:
    fleet, handles = _fleet_with_handles(make_cluster, h)
    target: Any = handles[-1]
    target.snapshot = lambda *_a, **_k: "uid"
    target.suspend = lambda **_k: "uid"
    target.resume = lambda **_k: True
    target.restore = lambda *_a, **_k: None

    def run() -> None:
      fleet.snapshot(target)
      fleet.suspend(target)
      fleet.resume(target)
      fleet.suspend(target)
      fleet.restore(target, "uid")
      assert not target._slot_released

    return run

  assert_o1("SandboxFleet snapshot, suspend, resume, restore", build, sizes)


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
    # The claim create is a round trip, so it must run with the fleet's lock free; under it
    # every other worker's acquire and release would wait out the request.
    _assert_unlocked("the per_claim create", fleet._lock)
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
    # At most one pod GET per step; dropping it would be an improvement.
    assert set(requests) <= {"read_namespaced_pod_status"} and len(requests) <= pod_gets, (
        f"{steps} steps sent {requests[:6]} (expected at most {pod_gets} pod GETs)")


def test_claim_watchdog_polls_send_one_pod_read_each(make_cluster: Any) -> None:
  # fail_fast is on by default, so every cold-starting per_claim acquire runs a ClaimWatchdog.
  # It resolves the claim's pod once (claim and Sandbox reads), then reads only the pod on
  # each poll.
  from agent_sandbox_rl.config import FailFastPolicy
  from agent_sandbox_rl.failfast import ClaimWatchdog
  cluster = make_cluster("solo")
  _route_pod_reads(cluster)
  cluster.custom_api.get_namespaced_custom_object.return_value = {
      "metadata": {"annotations": {}}, "status": {"sandbox": {"name": "sbx-1"}}}
  cluster.core_api.read_namespaced_pod_status.return_value = {
      "metadata": {"name": "sbx-1"}, "status": {"phase": "Pending"}}
  watchdog = ClaimWatchdog(cluster, "claim-1", FailFastPolicy())
  polls = 10
  for _ in range(polls):
    assert watchdog._check() is None
  requests = _cluster_requests(cluster)
  assert len(requests) <= 2 + polls, (
      f"{len(requests)} requests over {polls} watchdog polls (expected at most 2 + {polls}): "
      f"{requests[:6]}")


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
  """A session websocket that delivers ``chunks`` output chunks of ``size`` bytes on
  ``stream`` (stdout, stderr, or alternating), then the command's end marker on stdout."""

  def __init__(self, chunks: int, size: int, marker: str, stream: str = "stdout") -> None:
    def channel(i: int) -> str:
      return stream if stream != "interleaved" else ("stderr" if i % 2 else "stdout")
    self.pending = collections.deque(
        [(channel(i), "x" * size) for i in range(chunks)] + [("stdout", marker + "\r\n")])
    self.update_timeouts: list[float] = []

  def write_stdin(self, _data: str) -> None:
    pass

  def update(self, timeout: float) -> None:
    self.update_timeouts.append(timeout)

  def peek_stderr(self) -> bool:
    return bool(self.pending) and self.pending[0][0] == "stderr"

  def read_stderr(self) -> str:
    return self.pending.popleft()[1]

  def peek_stdout(self) -> bool:
    return bool(self.pending) and self.pending[0][0] == "stdout"

  def read_stdout(self) -> str:
    return self.pending.popleft()[1]

  def is_open(self) -> bool:
    return True


SESSION_CHUNKS = (200, 4000)
SESSION_CHUNK_BYTES = 1024
SESSION_RUN_TIMEOUT_S = 60.0


def _session(chunks: int, stream: str) -> SandboxSession:
  session = object.__new__(SandboxSession)
  session_any: Any = session
  session_any.pod, session_any.container, session_any._seq = "p", None, 0
  session_any._resp = _ChunkedResp(chunks, SESSION_CHUNK_BYTES, "ASRLDONE1X", stream)
  return session


@pytest.mark.parametrize("stream", ["stdout", "stderr", "interleaved"])
def test_session_run_cost_is_linear_in_output_size(stream: str) -> None:
  # F-39: SandboxSession.run (every command of a recycled sandbox with use_session=True)
  # must cost time linear in the command's output, whichever stream carries it. The
  # quadratic version re-joins and re-scans the whole buffer on every chunk, which is C
  # work: it executes no extra bytecode and its peak memory stays linear, so only time
  # shows it. It uses this process's CPU time, so other processes competing for the cores
  # don't count. The sizes differ 20x; linear is about 20x and quadratic about 400x, so the
  # 80x bound leaves 4x of room either way. Check 8's INV-PERF-HOTPATH-5 flags the same
  # pattern statically. The timeout only bounds a broken run; it never expires here.
  def run_once(chunks: int) -> float:
    session = _session(chunks, stream)
    started = time.process_time()
    out = session.run("true", timeout=SESSION_RUN_TIMEOUT_S)
    elapsed = time.process_time() - started
    assert len(out) == chunks * SESSION_CHUNK_BYTES
    return elapsed

  small = min(run_once(SESSION_CHUNKS[0]) for _ in range(7))
  # A quadratic run takes seconds at the large size, so stop at the first one already over.
  large = float("inf")
  for _ in range(3):
    large = min(large, run_once(SESSION_CHUNKS[1]))
    if large / small > 4 * SESSION_CHUNKS[1] / SESSION_CHUNKS[0]:
      break
  # Each wait for output must block up to a second (the command's own pace), not poll.
  probe = _session(3, stream)
  probe.run("true")
  update_timeouts = cast(_ChunkedResp, cast(Any, probe)._resp).update_timeouts
  assert min(update_timeouts) >= 1.0, f"SandboxSession.run polls every {min(update_timeouts)} s"
  size_ratio = SESSION_CHUNKS[1] / SESSION_CHUNKS[0]
  assert large / small <= 4 * size_ratio, (
      f"SandboxSession.run ({stream}): {small * 1e3:.2f} ms for {SESSION_CHUNKS[0]} chunks "
      f"vs {large * 1e3:.2f} ms for {SESSION_CHUNKS[1]} ({large / small:.0f}x for "
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


class _ExecutorFleet:
  """The fleet surface the executors use (process_parallel, the async fleet's, and the
  recycle executors), with free acquires and releases that count every claim and pool
  request, so only the executor's own per-task work (and the Observer's) is measured."""

  plan_ = None

  def __init__(self) -> None:
    from agent_sandbox_rl.observability import Observer
    observer: Any = Observer
    self._obs = observer()
    self.config = types.SimpleNamespace(max_concurrent=8, window_size=None)
    self.requests: collections.Counter[str] = collections.Counter()

  def acquire(self, _task: Task, **_kwargs: Any) -> Any:
    self.requests["acquire"] += 1
    return types.SimpleNamespace(cluster_name="solo", pod_name="p", claim_expires_at=None)

  def release(self, _handle: Any, **_kwargs: Any) -> None:
    self.requests["release"] += 1

  def warm_image(self, *_args: Any, **_kwargs: Any) -> None:
    self.requests["warm_image"] += 1

  def unwarm_image(self, *_args: Any, **_kwargs: Any) -> None:
    self.requests["unwarm_image"] += 1

  def set_pool_replicas(self, *_args: Any, **_kwargs: Any) -> None:
    self.requests["set_pool_replicas"] += 1

  def overcommit_guard(self, _expected: int) -> Any:
    return contextlib.nullcontext()


class _CleanReset:
  """A git-restore reset that always comes back clean, without an exec."""

  def prime(self, _handle: Any) -> Any:
    from agent_sandbox_rl.recycle import ResetBaseline
    return ResetBaseline(pristine_sha="sha")

  def recyclable(self, _baseline: Any) -> bool:
    return True

  def reset(self, _handle: Any, _baseline: Any, timeout: float) -> Any:
    from agent_sandbox_rl.recycle import ResetOutcome
    return ResetOutcome(clean=True, seconds=0.0)


EXECUTOR_TASKS = (1000, 20000)
EXECUTOR_IMAGES = 8
MAX_REUSES = 32
EXECUTORS = ("process_parallel-1", "process_parallel-8", "async_process_parallel",
             "recycle-1", "recycle-8", "async_recycle")


def _run_executor(executor: str, fleet: _ExecutorFleet, tasks: list[Task]) -> Any:
  from agent_sandbox_rl import recycle, strategies
  from agent_sandbox_rl.async_fleet import AsyncSandboxFleet
  run_tasks: Any = strategies.process_parallel
  run_recycled: Any = recycle.reuse_git_restore_sandbox
  run_recycled_async: Any = recycle.reuse_git_restore_sandbox_async

  def process(_task: Task, _handle: Any) -> str:
    return "ok"

  async def process_async(_task: Task, _handle: Any) -> str:
    return "ok"

  name, _, concurrency = executor.partition("-")
  recycled = {"reset": _CleanReset(), "max_reuses": MAX_REUSES, "use_session": False}
  if name == "process_parallel":
    return run_tasks(fleet, tasks, process, int(concurrency))
  if name == "recycle":
    return run_recycled(fleet, tasks, process, int(concurrency), **recycled)
  async_fleet: Any = object.__new__(AsyncSandboxFleet)
  async_fleet._fleet, async_fleet._executor = fleet, None
  try:
    if name == "async_process_parallel":
      return asyncio.run(async_fleet._process_parallel(tasks, process_async, 8))
    return asyncio.run(run_recycled_async(async_fleet, tasks, process_async, 8, **recycled))
  finally:
    async_fleet.close()


@pytest.mark.parametrize("executor", EXECUTORS)
def test_executor_cost_is_linear_in_task_count(executor: str) -> None:
  # The executors run every task of fleet.run(): process_parallel (and each window of the
  # windowed strategies), the async fleet's, and the recycle executors. Their per-task
  # bookkeeping must not grow with the task count. A lookup that scans the submitted futures
  # or the results is C work, invisible to instruction counts, so this uses CPU time (all
  # threads of this process). The sizes differ 20x; linear is about 20x and quadratic
  # hundreds, so the 80x bound leaves 4x of room either way. Claims stay at one per task, or
  # for recycling at one per image per MAX_REUSES tasks, with at most two pool requests
  # (unwarm on hold, re-warm on a re-claim) per claim.
  def run_once(n: int) -> float:
    fleet = _ExecutorFleet()
    tasks = [Task(id=f"t-{i}", image=f"img-{i % EXECUTOR_IMAGES}") for i in range(n)]
    started = time.process_time()
    results = _run_executor(executor, fleet, tasks)
    elapsed = time.process_time() - started
    assert results == ["ok"] * n
    claims = fleet.requests["acquire"]
    assert fleet.requests["release"] == claims, f"{executor}: {dict(fleet.requests)}"
    budget = n if "process_parallel" in executor else (
        EXECUTOR_IMAGES + -(-n // MAX_REUSES))
    pool_requests = sum(fleet.requests[k] for k in (
        "warm_image", "unwarm_image", "set_pool_replicas"))
    assert claims <= budget and pool_requests <= 2 * claims + EXECUTOR_IMAGES, (
        f"{executor}: {dict(fleet.requests)} for {n} tasks (at most {budget} claims)")
    return elapsed

  size_ratio = EXECUTOR_TASKS[1] / EXECUTOR_TASKS[0]
  small = min(run_once(EXECUTOR_TASKS[0]) for _ in range(5))
  large = float("inf")
  for _ in range(2):
    large = min(large, run_once(EXECUTOR_TASKS[1]))
    if large / small > 4 * size_ratio:
      break
  assert large / small <= 4 * size_ratio, (
      f"{executor}: {small * 1e3:.1f} ms for {EXECUTOR_TASKS[0]} tasks vs "
      f"{large * 1e3:.1f} ms for {EXECUTOR_TASKS[1]} ({large / small:.0f}x for "
      f"{size_ratio:.0f}x the tasks)")


class _WarmingFleet:
  """The fleet surface LookaheadWarmer uses, recording each pool request."""

  plan_ = object()                              # planned already, so reconcile never plans

  def __init__(self) -> None:
    self.config = types.SimpleNamespace(max_warmpool_size=8)
    self.requests: list[tuple[str, str]] = []

  def warm_image(self, image: str, **_kwargs: Any) -> None:
    self.requests.append(("warm", image))

  def set_pool_replicas(self, image: str, _replicas: int) -> None:
    self.requests.append(("scale", image))

  def unwarm_image(self, image: str) -> None:
    self.requests.append(("unwarm", image))


LOOKAHEAD_IMAGES = 50                          # per training batch; consecutive batches share half


def test_lookahead_sends_only_the_diff_and_keeps_no_history() -> None:
  # A streaming run adds each training batch to the LookaheadWarmer and finishes the one
  # before it. Each reconcile must send one pool request per image whose desired replicas
  # changed (create, scale, or remove) and none for the rest, and neither its cost nor its
  # tables may grow with the number of batches already streamed.
  from agent_sandbox_rl.lookahead import LookaheadWarmer
  fleet = _WarmingFleet()
  warmer: Any = LookaheadWarmer(fleet, register_images=False, max_workers=1)
  step = LOOKAHEAD_IMAGES // 2

  def counts(k: int) -> dict[str, int]:
    return {f"img-{i}": 1 for i in range(k * step, k * step + LOOKAHEAD_IMAGES)}

  def stream(k: int) -> None:
    calls: tuple[Callable[[], object], ...] = (
        lambda: warmer.add(k, counts(k)), lambda: warmer.finish(k - 1))
    for call in calls:
      before, sent = warmer.desired(), len(fleet.requests)
      call()
      after = warmer.desired()
      changed = {image for image in before.keys() | after.keys()
                 if before.get(image) != after.get(image)}
      requests = fleet.requests[sent:]
      assert sorted(image for _kind, image in requests) == sorted(changed), (
          f"batch {k}: {len(requests)} pool requests for {len(changed)} changed images")

  def table_sizes() -> dict[str, int]:
    return {name: len(value) for name, value in vars(warmer).items() if isinstance(value, Sized)}

  # Traced from the first batch, so the state each batch replaces is traced too; past batch
  # 100 the tables have reached their steady capacity, so what the next 300 batches keep is
  # what the warmer retains per batch.
  tracemalloc.start()
  try:
    warmer.add(0, counts(0))
    for k in range(1, 11):
      stream(k)
    early = count_bytecode_instructions(lambda: stream(11))
    early_tables = table_sizes()
    for k in range(12, 100):
      stream(k)
    fleet.requests.clear()                        # the recorder's own log grows by design
    gc.collect()
    before = tracemalloc.get_traced_memory()[0]
    for k in range(100, 400):
      stream(k)
    fleet.requests.clear()
    gc.collect()
    retained = (tracemalloc.get_traced_memory()[0] - before) / (400 - 100)
  finally:
    tracemalloc.stop()
  late = count_bytecode_instructions(lambda: stream(400))
  assert_scaling("LookaheadWarmer add + finish", early, late, (11, 400))
  grown = {name: (early_tables[name], size) for name, size in table_sizes().items()
           if size > early_tables.get(name, 0)}
  assert grown == {}, f"LookaheadWarmer tables grew with the stream: {grown}"
  assert retained < 4, f"LookaheadWarmer kept {retained:.1f} B per streamed batch"
  assert len(warmer._live) == 1 and len(warmer._active) == LOOKAHEAD_IMAGES, (
      f"{len(warmer._live)} live batches and {len(warmer._active)} active images "
      f"after 400 batches (expected 1 and {LOOKAHEAD_IMAGES})")


class _RecordingStop(threading.Event):
  """A stop event that records each wait's timeout instead of sleeping, and reports a stop
  on the ``stop_after``-th wait."""

  def __init__(self, stop_after: int) -> None:
    super().__init__()
    self.timeouts: list[float | None] = []
    self.stop_after = stop_after

  def wait(self, timeout: float | None = None) -> bool:
    self.timeouts.append(timeout)
    return len(self.timeouts) >= self.stop_after


def test_background_polls_wait_their_configured_interval(make_cluster: Any) -> None:
  # Each background loop sends Kubernetes requests per wakeup: the ClaimWatchdog that every
  # cold per_claim acquire starts (a pod GET per poll), the overcommit breaker (a filtered
  # pod list per poll), and, on 26/N, the idle BatchWatchdog. Each must sleep its full
  # configured interval between polls; a shorter wait multiplies the requests and
  # wakeups with nothing in the per-poll costs to show it.
  from agent_sandbox_rl import failfast, fleet as fleet_module
  from agent_sandbox_rl.config import FailFastPolicy
  from agent_sandbox_rl.failfast import ClaimWatchdog
  polls = 4
  policy = FailFastPolicy()
  watchdog = ClaimWatchdog(make_cluster("solo"), "claim-1", policy)
  claim_stop = _RecordingStop(polls + 1)
  checks = [0]

  def check() -> None:
    checks[0] += 1

  cast(Any, watchdog)._stop_evt = claim_stop
  cast(Any, watchdog)._check = check
  watchdog.run()
  assert claim_stop.timeouts == [policy.initial_delay_s] + [policy.poll_s] * polls, (
      f"ClaimWatchdog waited {claim_stop.timeouts} (expected {policy.initial_delay_s} s, "
      f"then {policy.poll_s} s per poll)")
  assert checks[0] == polls

  config = FleetConfig(install_teardown_hooks=False, max_live_sandboxes=100)
  fleet = SandboxFleet(config, registry=ClusterRegistry([make_cluster("solo")]))
  breaker_stop = _RecordingStop(polls + 1)
  counts = [0]

  def live_owned_count() -> int:
    counts[0] += 1
    return 0

  # Only the fleet module's Event: threading.Thread.start waits on an Event of its own.
  threading_for_fleet = types.SimpleNamespace(**vars(threading))
  threading_for_fleet.Event = lambda: breaker_stop
  with patch.object(fleet_module, "threading", threading_for_fleet), \
      patch.object(fleet, "live_owned_count", live_owned_count), \
      fleet.overcommit_guard(expected=1):
    deadline = time.monotonic() + 10
    while len(breaker_stop.timeouts) <= polls and time.monotonic() < deadline:
      time.sleep(0.01)
  assert breaker_stop.timeouts == [config.breaker_poll_s] * (polls + 1), (
      f"overcommit breaker waited {breaker_stop.timeouts} (expected {config.breaker_poll_s} s "
      f"per poll)")
  assert counts[0] == polls

  batch_watchdog_cls = getattr(failfast, "BatchWatchdog", None)
  if batch_watchdog_cls is None:
    return                                      # 26/N only, as in the idle-poll gate below
  batch_policy = types.SimpleNamespace(poll_s=1.0, initial_delay_s=5.0)
  batch_watchdog = batch_watchdog_cls(
      MagicMock(), _state_backed_batch(_acquired_state(4)), batch_policy, lambda *_a: None)
  batch_stop = _RecordingStop(polls)
  cast(Any, batch_watchdog)._stop_evt = batch_stop
  batch_watchdog.run()
  assert batch_stop.timeouts == [batch_policy.poll_s] * polls, (
      f"an idle BatchWatchdog waited {batch_stop.timeouts} (expected {batch_policy.poll_s} s)")


_EPISODE_CHURN_SCRIPT = """
import asyncio, contextlib, gc, itertools, logging, sys, threading, tracemalloc
sys.path.insert(0, sys.argv[4])
from conftest import FakeCluster
from k8s_agent_sandbox.batch_state import BatchState
from k8s_agent_sandbox.models import BatchGroup
from agent_sandbox_rl import BatchClaimConfig, ClusterRegistry, FleetConfig, SandboxFleet
from agent_sandbox_rl.batch_claims import BatchClaims
from agent_sandbox_rl.sources import Task
from agent_sandbox_rl.async_fleet import AsyncSandboxFleet
from agent_sandbox_rl.recycle import ResetBaseline, ResetOutcome, reuse_git_restore_sandbox
from agent_sandbox_rl.strategies import process_parallel

WARMUP, EPISODES = int(sys.argv[2]), int(sys.argv[3])
MODE, *FLAGS = sys.argv[1].split("+")
RECORDING = "recording" in FLAGS
EXECUTOR = next((flag for flag in FLAGS if flag in ("async", "recycle")), "parallel")
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


_episode_ids = itertools.count()


def next_task():
  # A distinct task per episode, as a real run has (one per dataset instance), so anything
  # keyed by task id (a metric label, a cache) grows here as it would in production.
  return Task(id=f"churn-{next(_episode_ids)}", image="img")


class CleanReset:
  def prime(self, handle):
    return ResetBaseline(pristine_sha="sha")

  def recyclable(self, baseline):
    return True

  def reset(self, handle, baseline, timeout):
    return ResetOutcome(clean=True, seconds=0.0)


_LOOP = asyncio.new_event_loop()
_ASYNC_FLEETS = {}


def process(_task, _handle):
  return "ok"


async def process_async(_task, _handle):
  return "ok"


def episode(fleet):
  # One task through an executor fleet.run() uses (process_parallel, the async fleet's, or
  # the recycle executor), so the per-task bookkeeping it adds (the process phase,
  # task_done, the run report) is measured too.
  task = next_task()
  if EXECUTOR == "async":
    if id(fleet) not in _ASYNC_FLEETS:
      async_fleet = object.__new__(AsyncSandboxFleet)
      async_fleet._fleet, async_fleet._executor = fleet, None
      # The pool starts its workers lazily, up to its cap; start them all now, so that
      # bounded thread growth is not read as memory kept per episode.
      pool = async_fleet._thread_pool()
      ready = threading.Barrier(pool._max_workers)
      for started in [pool.submit(ready.wait) for _ in range(pool._max_workers)]:
        started.result()
      _ASYNC_FLEETS[id(fleet)] = async_fleet
    run = _ASYNC_FLEETS[id(fleet)]._process_parallel([task], process_async, 1)
    result, = _LOOP.run_until_complete(run)
  elif EXECUTOR == "recycle":
    result, = reuse_git_restore_sandbox(fleet, [task], process, 1, reset=CleanReset(),
                                        use_session=False, scale_on_hold=False)
  else:
    result, = process_parallel(fleet, [task], process, 1)
  if result != "ok":
    sys.exit(f"episode failed: {result!r}")


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
      episode(fleet)
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
      episode(fleet)
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
  # The event loop and the async fleet's thread pool keep a bounded 10-20 KB of callback
  # state whatever the episode count (measured at 3000 and 12000 episodes), so the async mode
  # runs 4x the episodes to keep that offset under the bound; 8 B per episode still fails 2x.
  episodes = CHURN_EPISODES * (4 if "async" in mode else 1)
  proc = subprocess.run(
      [sys.executable, "-c", _EPISODE_CHURN_SCRIPT, mode, str(CHURN_WARMUP),
       str(episodes), str(tests_dir)],
      capture_output=True, text=True, env=env, timeout=300, check=False)
  assert proc.returncode == 0, proc.stderr[-2000:]
  return float(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "mode", ["claims", "fleet", "fleet+recording", "fleet+recording+async",
             "fleet+recording+recycle", "per_claim", "per_claim+recording"])
def test_fleet_episodes_retain_no_memory(mode: str) -> None:
  # Every RL episode acquires and releases one sandbox (a batch member in claim_mode="batch",
  # a claim of its own in per_claim mode); a long-lived fleet runs millions, so anything kept
  # per episode (a handed-out BatchClaim, a handle, a client-side claim record, a run-report
  # sample, a log record) grows without bound. fleet.run() always records a RunReport, so the
  # "+recording" modes run the episodes inside fleet.recording(). Episodes run through
  # process_parallel, or with "+async" / "+recycle" through the async fleet's executor or the
  # recycle executor. One reference is 8 B.
  per_episode = _retained_per_episode(mode)
  assert per_episode < 4, f"{per_episode:.1f} B/episode retained ({mode})"
