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

"""Deterministic performance and complexity invariants for the batch SDK.

Wall-clock benchmarks flake on shared CI runners and can't tell O(1) from O(N) at the sizes a
unit test affords. These tests measure cost in units that don't depend on the machine:

  - Suite A counts the Python bytecode instructions the test thread executes (``sys.monitoring``
    INSTRUCTION events on Python 3.12+, ``sys.settrace`` with ``f_trace_opcodes`` on 3.11) for
    one operation at a small and a large batch, and requires the ratio to stay near 1 for O(1)
    paths: BatchState transitions in every consumer mode and the done checks consumers make on
    every wake, watch event handling, ``connect()`` hits and misses, the per-episode warm
    ``acquire()`` and ``release_member()``, one fill create, one ``events()`` item, and per-pool
    release work. The O(1) gates also bound the peak bytes allocated, which catches
    whole-collection copies and sorts done by C builtins. The watch, ``connect()``, lookup, and
    fill gates assert that the call sends no Kubernetes request beyond its own watch stream or
    create, and the watch, acquire, and release gates that it wakes no other waiting acquire.
    LINE events are not enough, since a one-line comprehension over N members emits one LINE
    event regardless of N. A calibration test checks the counter on every available backend
    and its isolation from other threads, and every gate rejects implausibly small counts, so
    a broken counter fails instead of passing a 0 / 0 ratio. Suite A also counts ``__eq__``
    calls to expose O(S^2) list scans that run inside C.
  - Suite B pins outbound Kubernetes request counts to their algebraic lower bounds in the
    number of groups G, distinct templates T, and Lease renewals R, and pins batch create and
    delete call sites to DEBUG logging.
  - Suite C bounds memory with ``tracemalloc`` peaks, ``weakref`` liveness of list pages, a
    fully drained deletecollection stream, and the retained bytes per acquire and release
    episode of a long-lived dynamic batch, both in the batch state and in the handles.

A failing gate prints the measured counts at both sizes, so a regression from O(1) to O(N) is
reported as a ratio rather than a timeout, and a fix that restores O(1) turns the gate green.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import contextlib
import gc
import inspect
import logging
import os
import pathlib
import subprocess
import sys
import threading
import time
import tracemalloc
import unittest
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes import client as k8s_client

from k8s_agent_sandbox import async_sandbox_batch as async_sandbox_batch_module
from k8s_agent_sandbox import batch_state as batch_state_module
from k8s_agent_sandbox import sandbox_batch as sandbox_batch_module
from k8s_agent_sandbox.async_sandbox_batch import AsyncSandboxBatch
from k8s_agent_sandbox.batch_state import BatchState, ConsumerMode
from k8s_agent_sandbox.constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
)
from k8s_agent_sandbox.k8s_helper import K8sHelper
from k8s_agent_sandbox.models import BatchGroup, MemberState
from k8s_agent_sandbox.sandbox_batch import SandboxBatch
from k8s_agent_sandbox.test.unit.test_batch_fault_injection import (
    _new_stopped_async_batch,
    _new_stopped_sync_batch,
)

# An O(1) operation may execute a few more instructions at a larger size (e.g. longer ordinal
# strings), but never a multiple. A linear scan between the sizes below shows up as 10x to 50x.
O1_MAX_RATIO = 1.15
PER_POOL_MAX_RATIO = 1.25
# C builtins (dict(), sorted(), list.copy, `in` on a list) walk a collection without executing
# Python bytecode, so the O(1) gates also bound the operation's peak allocation: copying or
# sorting N entries allocates O(N) bytes. The bound is absolute, since a relative one lets a
# copy hide inside a large constant peak; measured noise between sizes is under 600 B.
# Non-allocating C scans (max(), any(), sum() over a table) are invisible to both counts, so
# the gates also swap every table that grows with the measured size (dicts, sets, lists, and
# deques on the batch state, the handle, and its warm credit) for a subclass that counts whole
# walks, and require zero. Check 8 in verify_batch_static.py flags the same scans statically in
# the methods it audits, including tables passed whole to module-level helpers, which it can't
# see into. A walk over a table the gate doesn't swap (one not held as a plain attribute, or one
# smaller than the gate's small size) goes unseen by both.
PEAK_SLACK_BYTES = 4096
# Every gated operation executes well over this many instructions. A smaller count means the
# counter is broken, and a broken counter must fail rather than pass a 0 / 0 ratio.
MIN_PLAUSIBLE_COUNT = 20


class InstructionCounter:
    """Counts the Python bytecode instructions the current thread executes inside the block.

    Uses ``sys.monitoring`` INSTRUCTION events on Python 3.12+ and ``sys.settrace`` opcode events
    on 3.11 (or when ``backend="settrace"``). ``sys.monitoring`` is process-wide, so the callback
    drops instructions from other threads (e.g. a test HTTP server's ``serve_forever`` loop),
    which would otherwise inflate either size at random. ``settrace`` is per thread on 3.11, but
    on 3.12+ it shares instrumented code objects across threads and undercounts code another
    thread runs concurrently, so it is the default only on 3.11.
    """

    def __init__(self, backend: str | None = None) -> None:
        if backend is None:
            backend = "monitoring" if hasattr(sys, "monitoring") else "settrace"
        self.backend = backend
        self.count = 0
        self._owner = threading.get_ident()
        self._tool_id: int | None = None
        self._previous_trace: Any = None

    def _on_instruction(self, _code: object, _offset: int) -> None:
        if threading.get_ident() == self._owner:
            self.count += 1

    def _tracer(self, frame: Any, event: str, _arg: object) -> Any:
        # f_trace must be set before f_trace_opcodes takes effect on 3.12+.
        frame.f_trace = self._tracer
        frame.f_trace_opcodes = True
        frame.f_trace_lines = False
        if event == "opcode":
            self.count += 1
        return self._tracer

    def __enter__(self) -> InstructionCounter:
        self._owner = threading.get_ident()
        if self.backend == "monitoring":
            monitoring: Any = getattr(sys, "monitoring")
            for candidate in (5, 4, 3, 2, 0):
                try:
                    monitoring.use_tool_id(candidate, "batch_perf_invariants")
                except ValueError:
                    continue
                self._tool_id = candidate
                break
            if self._tool_id is None:
                raise RuntimeError("no free sys.monitoring tool id")
            instruction = monitoring.events.INSTRUCTION
            monitoring.register_callback(self._tool_id, instruction, self._on_instruction)
            monitoring.set_events(self._tool_id, instruction)
        else:
            self._previous_trace = sys.gettrace()
            sys.settrace(self._tracer)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.backend == "monitoring":
            monitoring: Any = getattr(sys, "monitoring")
            assert self._tool_id is not None
            monitoring.set_events(self._tool_id, 0)
            monitoring.register_callback(self._tool_id, monitoring.events.INSTRUCTION, None)
            monitoring.free_tool_id(self._tool_id)
            self._tool_id = None
        else:
            sys.settrace(self._previous_trace)


def count_bytecode_instructions(fn: Callable[[], object], backend: str | None = None) -> int:
    """Returns the number of Python bytecode instructions the calling thread executes in
    ``fn()``."""
    with InstructionCounter(backend) as counter:
        fn()
    return counter.count


def measure_peak(build: Callable[[], Callable[[], object]], repeats: int = 3) -> int:
    """Warms the operation up, then returns its minimum peak allocation in bytes over
    ``repeats`` fresh builds."""
    build()()
    peaks = []
    for _ in range(repeats):
        fn = build()
        with MemoryWindow() as window:
            fn()
            peaks.append(window.peak())
    return min(peaks)


def assert_peak_scaling(
    case: unittest.TestCase, label: str, small: int, large: int, sizes: str
) -> None:
    """Fails when the larger size allocates a multiple of the smaller one, which means the
    operation copies or sorts a whole collection in C."""
    case.assertLessEqual(
        large - small,
        PEAK_SLACK_BYTES,
        f"{label}: peak {small:,} B at {sizes.split('|')[0]} vs {large:,} B at "
        f"{sizes.split('|')[1]} (a C-level copy or sort of the whole collection)",
    )


# Whole-table walks seen during measure()'s counted runs, checked by the gate that called it.
_MEASURED_WALKS: list[int] = []


def measure(build: Callable[[], Callable[[], object]], repeats: int = 3) -> int:
    """Warms the operation up on one fresh build, then returns the minimum count over
    ``repeats`` fresh builds, so one-time work (lazy imports, pydantic schema caches) and any
    stray interference only ever inflate a sample, never the result. Each fresh build runs
    once, so no run can hide work that only some calls do."""
    build()()
    counts = []
    for _ in range(repeats):
        fn = build()
        _WalkCountingDict.walks = 0
        counts.append(count_bytecode_instructions(fn))
        _MEASURED_WALKS.append(_WalkCountingDict.walks)
    return min(counts)


def _assert_no_measured_walks(case: unittest.TestCase, label: str) -> None:
    walks = list(_MEASURED_WALKS)
    _MEASURED_WALKS.clear()
    case.assertEqual(
        sum(walks), 0,
        f"{label}: walked a whole table {walks} times across the measured runs (a C-level "
        f"scan that executes no bytecode and allocates nothing)",
    )


def _ready_claim(batch_id: str, ordinal: int, pool: str, group_size: int) -> dict[str, Any]:
    return {
        "metadata": {
            "name": f"{batch_id}-{ordinal}",
            "uid": f"uid-{ordinal}",
            "resourceVersion": str(1000 + ordinal),
            "annotations": {
                BATCH_GROUP_SIZE_ANNOTATION: str(group_size),
                BATCH_GROUP_MIN_READY_ANNOTATION: str(group_size),
            },
        },
        "spec": {"warmPoolRef": {"name": pool}},
        "status": {
            "sandbox": {
                "name": f"sbx-{ordinal}",
                "podIPs": [f"10.{ordinal // 65536}.{(ordinal // 256) % 256}.{ordinal % 256}"],
            },
            "conditions": [{"type": "Ready", "status": "True", "reason": "SandboxReady"}],
        },
    }


def _not_ready(obj: dict[str, Any]) -> dict[str, Any]:
    flapped = {**obj, "status": {**obj["status"]}}
    flapped["status"]["conditions"] = [
        {"type": "Ready", "status": "False", "reason": "SandboxNotReady"}
    ]
    flapped["metadata"] = {
        **obj["metadata"],
        "resourceVersion": str(int(obj["metadata"]["resourceVersion"]) + 100000),
    }
    return flapped


def _filled_state(n: int, g: int, *, skip_last: bool = False) -> BatchState:
    """Returns a ``BatchState`` of ``g`` groups holding ``n`` Ready fill members in total."""
    per = n // g
    state = BatchState("bperf", [BatchGroup(warmpool=f"pool-{i}", size=per) for i in range(g)])
    for idx in range(n - 1 if skip_last else n):
        state.upsert_claim(_ready_claim("bperf", idx, f"pool-{idx // per}", per))
    _count_walks_of_large_tables([state], min(g, 20))
    return state


def _consumer_state(n: int, g: int, mode: ConsumerMode) -> tuple[BatchState, dict[str, Any]]:
    """Returns a ``BatchState`` of ``g`` groups (``min_ready`` = size) in consumer ``mode``,
    filled except for the last member of the first and the last group, plus the claim whose
    ADDED event completes the first group. In QUORUM mode that event passes the per-group check
    and reaches the cross-group one while the last group still holds the quorum back, so a scan
    over the groups in order would walk all of them before finding the short one."""
    per = n // g
    groups = [BatchGroup(warmpool=f"pool-{i}", size=per, min_ready=per) for i in range(g)]
    state = BatchState("bperf", groups)
    held_back = {per - 1, n - 1}
    for idx in range(n):
        if idx not in held_back:
            state.upsert_claim(_ready_claim("bperf", idx, f"pool-{idx // per}", per))
    state.set_mode(mode)
    _count_walks_of_large_tables([state], min(g, 20))
    return state, _ready_claim("bperf", per - 1, "pool-0", per)


def _seed_release_history(state: BatchState, pool: str, releases: int) -> None:
    """Runs ``releases`` acquire and release episodes on ``pool``, so the batch carries that
    much release history (tombstones, delete sequences) into the measured operation."""
    for i in range(releases):
        name = state.start_acquire(pool)
        obj = _ready_claim("hist", i, pool, 0)
        obj["metadata"] = {"name": name, "uid": f"uid-{name}", "resourceVersion": str(i + 1)}
        state.upsert_claim(obj)
        state.acquired_member(name)
        state.release_claim(name, pool)
        state.note_deleted(name, state.delete_seq(name))


def _single_group_ready(n: int) -> Callable[[], object]:
    state = _filled_state(n, 1, skip_last=True)
    obj = _ready_claim("bperf", n - 1, "pool-0", n)
    return lambda: state.upsert_claim(obj)


def _single_group_flap(n: int) -> Callable[[], object]:
    state = _filled_state(n, 1)
    obj = _not_ready(_ready_claim("bperf", n // 2, "pool-0", n))
    return lambda: state.upsert_claim(obj)


def _single_group_lost(n: int) -> Callable[[], object]:
    state = _filled_state(n, 1)
    return lambda: state.mark_lost(f"bperf-{n // 2}")


def _ratio_message(label: str, small: int, large: int, sizes: str) -> str:
    return (
        f"{label}: {small:,} instructions at {sizes.split('|')[0]} vs {large:,} at "
        f"{sizes.split('|')[1]} (ratio {large / max(small, 1):.2f}x)"
    )


def assert_scaling(
    case: unittest.TestCase, label: str, small: int, large: int, max_ratio: float, sizes: str
) -> None:
    """Fails when the larger size costs more than ``max_ratio`` times the smaller one, or when
    the smaller count is implausibly low, which means the counter itself is broken."""
    case.assertGreaterEqual(
        small, MIN_PLAUSIBLE_COUNT, f"{label}: counted only {small} instructions; broken counter"
    )
    case.assertLessEqual(large / small, max_ratio, _ratio_message(label, small, large, sizes))


class TestSuiteABatchStateComplexity(unittest.TestCase):
    """Suite A: BatchState transitions run once per watch event, acquire, or release, so their
    instruction counts must not grow with the batch's member count N or group count G."""

    SIZES = ((100, 20), (5000, 1000))

    def _assert_o1(
        self,
        label: str,
        build: Callable[[int, int], Callable[[], object]],
        sizes: tuple[tuple[int, int], tuple[int, int]] | None = None,
    ) -> None:
        (n_small, g_small), (n_large, g_large) = sizes or self.SIZES
        size_label = f"N={n_small},G={g_small}|N={n_large},G={g_large}"
        _MEASURED_WALKS.clear()
        small = measure(lambda: build(n_small, g_small))
        large = measure(lambda: build(n_large, g_large))
        assert_scaling(self, label, small, large, O1_MAX_RATIO, size_label)
        _assert_no_measured_walks(self, label)
        small_peak = measure_peak(lambda: build(n_small, g_small))
        large_peak = measure_peak(lambda: build(n_large, g_large))
        assert_peak_scaling(self, label, small_peak, large_peak, size_label)

    def test_upsert_claim_ready_and_flap_are_o1(self) -> None:
        def ready(n: int, g: int) -> Callable[[], object]:
            state = _filled_state(n, g, skip_last=True)
            obj = _ready_claim("bperf", n - 1, f"pool-{g - 1}", n // g)
            return lambda: state.upsert_claim(obj)

        def flap(n: int, g: int) -> Callable[[], object]:
            state = _filled_state(n, g)
            obj = _not_ready(_ready_claim("bperf", n - 1, f"pool-{g - 1}", n // g))
            return lambda: state.upsert_claim(obj)

        self._assert_o1("upsert_claim(Ready ADDED)", ready)
        self._assert_o1("upsert_claim(Ready flap to False)", flap)

    def test_mark_lost_pop_event_and_pop_group_outcome_are_o1(self) -> None:
        def lost(n: int, g: int) -> Callable[[], object]:
            state = _filled_state(n, g)
            return lambda: state.mark_lost(f"bperf-{n - 1}")

        def pops(n: int, g: int) -> Callable[[], object]:
            state = _filled_state(n, g)
            state.mark_lost(f"bperf-{n - 1}")

            def run() -> None:
                state.pop_event()
                state.pop_group_outcome()

            return run

        self._assert_o1("mark_lost", lost)
        self._assert_o1("pop_event + pop_group_outcome", pops)

    def test_acquire_and_release_claim_transitions_are_o1(self) -> None:
        def episode(n: int, g: int) -> Callable[[], object]:
            # Both N members and N earlier releases, so per-release work can't grow with either
            # the batch or its release history (F-35's tombstones keep growing).
            state = _filled_state(n, g)
            pool = f"pool-{g // 2}"
            _seed_release_history(state, pool, n)

            def run() -> None:
                name = state.start_acquire(pool)
                obj = _ready_claim("bperf", 0, pool, 0)
                obj["metadata"] = {"name": name, "uid": f"uid-{name}", "resourceVersion": "7"}
                state.upsert_claim(obj)
                state.acquired_member(name)
                # release_member is what the handles call; it wraps release_claim.
                state.release_member(name)
                state.note_deleted(name, state.delete_seq(name))

            return run

        self._assert_o1("acquire -> upsert -> acquired -> release -> note_deleted", episode)

    def test_ready_event_is_o1_in_every_consumer_mode(self) -> None:
        # Once events(), group_outcomes(), or wait_for_quorum() fixes the mode, each fill event
        # also routes the member and checks its group outcome or the quorum.
        for mode in (ConsumerMode.STREAM, ConsumerMode.GROUP, ConsumerMode.QUORUM):

            def ready(n: int, g: int, mode: ConsumerMode = mode) -> Callable[[], object]:
                state, obj = _consumer_state(n, g, mode)
                return lambda: state.upsert_claim(obj)

            with self.subTest(mode=mode.value):
                self._assert_o1(f"upsert_claim(Ready, {mode.value} mode)", ready)

    def test_consumer_done_checks_are_o1_in_every_consumer_mode(self) -> None:
        # A waiting consumer rechecks events_done() or groups_done() on every wake, and every
        # watch event wakes it, so these run per event too.
        for mode in (ConsumerMode.STREAM, ConsumerMode.GROUP, ConsumerMode.QUORUM):

            def done(n: int, g: int, mode: ConsumerMode = mode) -> Callable[[], object]:
                state, _obj = _consumer_state(n, g, mode)
                while state.pop_event() is not None:
                    pass
                while state.pop_group_outcome() is not None:
                    pass
                return lambda: (state.events_done(), state.groups_done(), state.fill_settled())

            with self.subTest(mode=mode.value):
                self._assert_o1(f"events_done/groups_done/fill_settled ({mode.value})", done)

    def test_fill_events_are_o1_in_one_large_group(self) -> None:
        # The common batch is one group of N claims for one image, so per-event work must not
        # grow with the group's own size either.
        cases: list[tuple[str, Callable[[int], Callable[[], object]]]] = [
            ("upsert_claim(Ready ADDED, 1 group)", _single_group_ready),
            ("upsert_claim(Ready flap, 1 group)", _single_group_flap),
            ("mark_lost(1 group)", _single_group_lost),
        ]
        for label, single in cases:

            def build(
                n: int, _g: int, single: Callable[[int], Callable[[], object]] = single
            ) -> Callable[[], object]:
                return single(n)

            with self.subTest(op=label):
                self._assert_o1(label, build, sizes=((100, 1), (5000, 1)))

    def test_get_member_is_o1(self) -> None:
        def lookup(n: int, g: int) -> Callable[[], object]:
            state = _filled_state(n, g)
            return lambda: state.get_member(f"bperf-{n // 2}")

        self._assert_o1("get_member", lookup)

    def test_has_pool_is_o1_in_batch_and_group_count(self) -> None:
        # F-31: has_pool() must not scan every group and member. Its callers are the handles'
        # release_group(), so a scan makes releasing all G pools of a multi-image batch one
        # by one cost O(G * N) under the batch lock before each pool's deletecollection.
        def probe(n: int, g: int) -> Callable[[], object]:
            state = _filled_state(n, g)
            return lambda: state.has_pool("pool-not-in-batch")

        self._assert_o1("has_pool(unknown pool)", probe)

    def test_per_pool_members_and_release_group_scale_with_pool_size(self) -> None:
        # F-31: members(warmpool) and release_group(warmpool) on a one-member pool must not scan
        # the other G - 1 pools' members (the same O(G * N) release-all-pools cost as has_pool).
        sizes = (100, 5000)

        def one_member_pools(g: int) -> BatchState:
            groups = [BatchGroup(warmpool=f"pool-{i}", size=1) for i in range(g)]
            state = BatchState("bperf", groups)
            for idx in range(g):
                state.upsert_claim(_ready_claim("bperf", idx, f"pool-{idx}", 1))
            _count_walks_of_large_tables([state], sizes[0])
            return state

        for label, op in (
            ("members('pool-50')", lambda s: (lambda: s.members("pool-50"))),
            ("release_group('pool-50')", lambda s: (lambda: s.release_group("pool-50"))),
        ):
            _MEASURED_WALKS.clear()
            small = measure(lambda: op(one_member_pools(sizes[0])))
            large = measure(lambda: op(one_member_pools(sizes[1])))
            with self.subTest(op=label):
                size_label = f"G={sizes[0]} one-member pools|G={sizes[1]}"
                assert_scaling(self, label, small, large, PER_POOL_MAX_RATIO, size_label)
                _assert_no_measured_walks(self, label)
                small_peak = measure_peak(lambda: op(one_member_pools(sizes[0])))
                large_peak = measure_peak(lambda: op(one_member_pools(sizes[1])))
                assert_peak_scaling(self, label, small_peak, large_peak, size_label)


class TestInstructionCounterCalibration(unittest.TestCase):
    """The gates trust the counter, so check it counts work on every backend this interpreter
    has, and ignores other threads."""

    @staticmethod
    def _loop(n: int) -> None:
        total = 0
        for i in range(n):
            total += i

    def _backends(self) -> list[str]:
        return ["monitoring", "settrace"] if hasattr(sys, "monitoring") else ["settrace"]

    def test_counts_scale_with_work_on_every_backend(self) -> None:
        for backend in self._backends():
            with self.subTest(backend=backend):
                one = count_bytecode_instructions(lambda: self._loop(1000), backend)
                two = count_bytecode_instructions(lambda: self._loop(2000), backend)
                self.assertGreaterEqual(one, 1000)
                self.assertAlmostEqual(two / one, 2.0, delta=0.05)

    def test_ignores_instructions_from_other_threads(self) -> None:
        stop = threading.Event()

        def spin() -> None:
            while not stop.is_set():
                self._loop(100)

        quiet = count_bytecode_instructions(lambda: self._loop(1000))
        spinner = threading.Thread(target=spin, daemon=True)
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-5)
        spinner.start()
        try:
            noisy = count_bytecode_instructions(lambda: self._loop(1000))
        finally:
            stop.set()
            spinner.join()
            sys.setswitchinterval(previous)
        self.assertEqual(noisy, quiet)


def _seed_acquired(state: BatchState, n: int, pool: str) -> list[str]:
    """Adds ``n`` Ready members to ``state`` through the dynamic acquire path."""
    names = []
    for i in range(n):
        name = state.start_acquire(pool)
        obj = _ready_claim("seed", i, pool, 0)
        obj["metadata"] = {"name": name, "uid": f"uid-{name}", "resourceVersion": str(i + 1)}
        state.upsert_claim(obj)
        assert state.acquired_member(name) is not None
        names.append(name)
    return names


def _modified_event(state: BatchState, name: str) -> dict[str, Any]:
    member = state.get_member(name)
    assert member is not None
    obj = _ready_claim("seed", 0, member.warmpool, 0)
    obj["metadata"] = {"name": name, "uid": f"uid-{name}", "resourceVersion": "999999"}
    obj["status"]["sandbox"] = {"name": member.sandbox_name, "podIPs": list(member.pod_ips)}
    return {"type": "MODIFIED", "object": obj}


class _CountingEvent(threading.Event):
    """A waiter that counts how often it is woken."""

    sets = 0

    def set(self) -> None:
        type(self).sets += 1
        super().set()


class _CountingAsyncEvent(asyncio.Event):
    sets = 0

    def set(self) -> None:
        type(self).sets += 1
        super().set()


class _WalkCountingDict(dict[Any, Any]):
    """A dict whose whole-table walks are Python calls, so a test can count the C-level ones
    (``max(d.values())``, ``any(d)``, ``dict(d)``, ``d == other``) that execute no bytecode of
    their own. ``walks`` is shared by every counting container below."""

    walks = 0


def _count_walks_through(cls: type, names: tuple[str, ...]) -> type:
    """Makes each of ``names`` on ``cls`` count one whole-table walk, then defer to the base
    container. Only methods whose cost grows with the container itself are listed: `s.update(x)`
    and `s >= x` walk `x`, not `s`, so they aren't."""
    base = cls.__mro__[1]
    for name in names:
        def walking(self: Any, *args: Any, _name: str = name, **kwargs: Any) -> Any:
            _WalkCountingDict.walks += 1
            return getattr(base, _name)(self, *args, **kwargs)

        setattr(cls, name, walking)
    return cls


_count_walks_through(
    _WalkCountingDict,
    ("__iter__", "keys", "values", "items", "copy", "__or__", "__ror__", "__reversed__",
     "__eq__", "__ne__"),
)


class _WalkCountingSet(set[Any]):
    pass


_count_walks_through(
    _WalkCountingSet,
    # `s <= x` and `s < x` test every element of s; union, difference, and symmetric
    # difference copy s; `s &= x` rebuilds s.
    ("__iter__", "copy", "issubset", "__le__", "__lt__", "__eq__", "__ne__", "union",
     "__or__", "__ror__", "difference", "__sub__", "symmetric_difference", "__xor__",
     "__rxor__", "intersection_update", "__iand__"),
)


class _WalkCountingList(list[Any]):
    pass


_count_walks_through(
    _WalkCountingList,
    ("__iter__", "copy", "__contains__", "index", "count", "remove", "__reversed__", "sort",
     "reverse", "__add__", "__mul__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__",
     "__ge__"),
)


class _WalkCountingDeque(collections.deque[Any]):
    pass


_count_walks_through(
    _WalkCountingDeque,
    ("__iter__", "copy", "__contains__", "index", "count", "remove", "__reversed__", "reverse",
     "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__"),
)


def _seed_known_pools(batch: Any, p: int) -> None:
    """Gives the handle ``p`` other already-checked pools with warm credit, as one RL batch
    per cluster acquiring from one pool per image accumulates, and makes every table that now
    holds an entry per pool count its whole walks."""
    for i in range(p):
        pool = f"other-pool-{i}"
        if isinstance(batch._checked_pools, set):
            batch._checked_pools.add(pool)
        else:
            batch._checked_pools.append(pool)
        batch._warm_credit.record(pool, 1)
    _count_walks_of_large_tables([batch, batch._warm_credit, batch._state], p)


def _count_walks_of_large_tables(owners: list[Any], at_least: int) -> list[str]:
    """Swaps every plain dict, set, list, or deque attribute of ``owners`` holding at least
    ``at_least`` entries for a walk-counting copy, and returns the swapped attribute names."""
    counting: dict[type, type] = {
        dict: _WalkCountingDict, set: _WalkCountingSet, list: _WalkCountingList,
        collections.deque: _WalkCountingDeque,
    }
    swapped = []
    for owner in owners:
        for attr, value in list(vars(owner).items()):
            wrapper = counting.get(type(value))
            if wrapper is None or len(value) < at_least:
                continue
            setattr(owner, attr, wrapper(value))
            swapped.append(f"{type(owner).__name__}.{attr}")
    return swapped


def _groups_for(n: int) -> list[BatchGroup]:
    """``pool-a`` plus enough empty groups that the batch has ``n // 5`` groups."""
    return [BatchGroup(warmpool="pool-a", size=1)] + [
        BatchGroup(warmpool=f"grp-{i}", size=0) for i in range(n // 5 - 1)
    ]


class _HandleGateBase(unittest.IsolatedAsyncioTestCase):
    """Builds batches for the handle gates and samples instructions and peak allocation.

    ``_sample_sync``/``_sample_async`` reuse one batch for the warmup and every sample, which
    suits calls that change the batch the same way each time (an acquire, a connect miss) or
    not at all (a lookup). ``_sample_fresh_sync``/``_sample_fresh_async`` build a fresh batch
    for every run, for transitions that happen only once per batch (a DELETED event, a pool's
    release), so each counted run does the real work rather than a repeat that finds nothing
    to do.
    """

    SIZES = (100, 2000)
    REPEATS = 3

    def _sync_batch(self, n: int) -> tuple[Any, SandboxBatch, list[str]]:
        """Returns a batch of ``n // 5`` groups with ``n`` acquired, connected members in
        ``pool-a``, ``n`` other known pools, and ``n`` past releases, so every handle gate
        scales the member, connection, group, pool, and release-history counts together, and
        every table holding ``n`` entries (or the ``n // 5`` groups) counts its whole walks."""
        _client, helper, batch = _new_stopped_sync_batch(_groups_for(n), batch_id="b-hot")
        names = _seed_acquired(batch._state, n, "pool-a")
        for name in names:
            member = batch._state.get_member(name)
            assert member is not None
            batch.connect(member)
        _seed_known_pools(batch, n)
        # Release history (tombstones, delete sequences) grows with n too.
        _seed_release_history(batch._state, "pool-history", n)
        _count_walks_of_large_tables([batch._state], n // 5)
        helper.reset_mock()
        return helper, batch, names

    async def _async_batch(self, n: int) -> tuple[Any, AsyncSandboxBatch, list[str]]:
        _client, helper, batch = await _new_stopped_async_batch(_groups_for(n), batch_id="b-hot")
        names = _seed_acquired(batch._state, n, "pool-a")
        for name in names:
            member = batch._state.get_member(name)
            assert member is not None
            await batch.connect(member)
        _seed_known_pools(batch, n)
        # Release history (tombstones, delete sequences) grows with n too.
        _seed_release_history(batch._state, "pool-history", n)
        _count_walks_of_large_tables([batch._state], n // 5)
        helper.reset_mock()
        return helper, batch, names

    def _waiters(self, batch: Any, n: int, cls: type) -> None:
        """Puts ``n`` other acquires in flight, each with its parked waiter, as concurrent RL
        workers would, so both the batch state's in-flight table and the handle's waiters
        grow with ``n``."""
        for _ in range(n):
            name = batch._state.start_acquire("pool-waiting")
            batch._acquire_waiters[name] = cls()
        _count_walks_of_large_tables([batch, batch._state], n)

    def _sample_sync(self, run: Callable[[], object]) -> tuple[int, int]:
        """Returns ``(min instructions, min peak bytes)`` for ``run`` after one warmup."""
        run()
        counts, peaks = [], []
        _WalkCountingDict.walks = 0
        # Back-to-back runs, keeping the largest, so work done on only some calls (every other
        # one, say) can't hide in an uncounted run or behind a cheaper sample.
        for _ in range(self.REPEATS):
            counts.append(count_bytecode_instructions(run))
        for _ in range(self.REPEATS):
            with MemoryWindow() as window:
                run()
                peaks.append(window.peak())
        self._walks.append(_WalkCountingDict.walks)
        return max(counts), max(peaks)

    async def _sample_async(self, run: Callable[[], Awaitable[object]]) -> tuple[int, int]:
        await run()
        counts, peaks = [], []
        _WalkCountingDict.walks = 0
        for _ in range(self.REPEATS):
            with InstructionCounter() as counter:
                await run()
            counts.append(counter.count)
        for _ in range(self.REPEATS):
            with MemoryWindow() as window:
                await run()
                peaks.append(window.peak())
        self._walks.append(_WalkCountingDict.walks)
        return max(counts), max(peaks)

    def _sample_fresh_sync(self, build: Callable[[], Callable[[], object]]) -> tuple[int, int]:
        """Like ``_sample_sync``, but runs each sample on its own freshly built batch."""
        build()()
        counts, peaks = [], []
        walks = 0
        for _ in range(self.REPEATS):
            run = build()
            _WalkCountingDict.walks = 0
            counts.append(count_bytecode_instructions(run))
            walks += _WalkCountingDict.walks
            run = build()
            _WalkCountingDict.walks = 0
            with MemoryWindow() as window:
                run()
                peaks.append(window.peak())
            walks += _WalkCountingDict.walks
        self._walks.append(walks)
        return min(counts), min(peaks)

    async def _sample_fresh_async(
        self, build: Callable[[], Awaitable[Callable[[], Awaitable[object]]]]
    ) -> tuple[int, int]:
        await (await build())()
        counts, peaks = [], []
        walks = 0
        for _ in range(self.REPEATS):
            run = await build()
            _WalkCountingDict.walks = 0
            with InstructionCounter() as counter:
                await run()
            counts.append(counter.count)
            walks += _WalkCountingDict.walks
            run = await build()
            _WalkCountingDict.walks = 0
            with MemoryWindow() as window:
                await run()
                peaks.append(window.peak())
            walks += _WalkCountingDict.walks
        self._walks.append(walks)
        return min(counts), min(peaks)

    def setUp(self) -> None:
        super().setUp()
        # Whole-table walks seen during each sample's measured runs (see _WalkCountingDict).
        self._walks: list[int] = []

    def _assert_flat(
        self, label: str, small: tuple[int, int], large: tuple[int, int]
    ) -> None:
        sizes = f"N={self.SIZES[0]}|N={self.SIZES[1]}"
        assert_scaling(self, label, small[0], large[0], O1_MAX_RATIO, sizes)
        assert_peak_scaling(self, label, small[1], large[1], sizes)
        self._assert_no_walks(label)

    def _assert_no_walks(self, label: str) -> None:
        walks, self._walks = self._walks, []
        self.assertEqual(
            sum(walks), 0,
            f"{label}: walked a whole per-member or per-pool table {walks} times across the "
            f"measured runs (a C-level O(N) or O(P) scan, invisible to both counts)",
        )


class TestSuiteAHandleHotPaths(_HandleGateBase):
    """Suite A: a ``connect()`` cache hit on the sync and async batch handles stays flat as the
    batch and its connection cache grow, and sends no Kubernetes request. A hit leaves the batch
    unchanged, so every sample measures the same state. Watch events are gated in
    ``TestSuiteAWatchEventKinds``.
    """

    def test_sync_connect_cache_hit_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            helper, batch, names = self._sync_batch(n)
            member = batch._state.get_member(names[n // 2])
            assert member is not None
            cached = batch.connect(member)
            hit = member
            samples.append(self._sample_sync(lambda: batch.connect(hit)))
            self.assertIs(batch.connect(member), cached)
            self.assertEqual(helper.method_calls, [], "connect() cache hit sent requests")
        self._assert_flat("SandboxBatch.connect (cache hit)", *samples)

    async def test_async_connect_cache_hit_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            helper, batch, names = await self._async_batch(n)
            member = batch._state.get_member(names[n // 2])
            assert member is not None
            cached = await batch.connect(member)
            hit = member

            async def run() -> None:
                await batch.connect(hit)

            samples.append(await self._sample_async(run))
            self.assertIs(await batch.connect(member), cached)
            self.assertEqual(helper.mock_calls, [], "connect() cache hit sent requests")
        self._assert_flat("AsyncSandboxBatch.connect (cache hit)", *samples)


class TestSuiteAHandleLookups(_HandleGateBase):
    """Suite A: the handle reads RL makes at every step boundary (``SandboxHandle.is_alive``
    through ``BatchClaim.still_reaches``) stay flat as the batch grows: a by-name member lookup,
    for both a live member and one the batch no longer has, and ``err()``.

    ``get_member()`` is the candidate F-30 fix; on a branch without it the lookup gates skip and
    the RL suite's ``still_reaches`` gates carry F-30.
    """

    def _lookups(self, batch: Any, names: list[str]) -> Callable[[], object]:
        live, gone = names[len(names) // 2], "not-a-member"

        def run() -> None:
            assert batch.get_member(live) is not None
            assert batch.get_member(gone) is None

        return run

    def test_sync_get_member_is_o1(self) -> None:
        if not hasattr(SandboxBatch, "get_member"):
            self.skipTest("SandboxBatch.get_member() is not on this branch (F-30 fix adds it)")
        samples = []
        for n in self.SIZES:
            helper, batch, names = self._sync_batch(n)
            samples.append(self._sample_sync(self._lookups(batch, names)))
            self.assertEqual(helper.method_calls, [], "get_member() sent requests")
        self._assert_flat("SandboxBatch.get_member (live and missing)", *samples)

    async def test_async_get_member_is_o1(self) -> None:
        if not hasattr(AsyncSandboxBatch, "get_member"):
            self.skipTest("AsyncSandboxBatch.get_member() is not on this branch (F-30 fix adds it)")
        samples = []
        for n in self.SIZES:
            helper, batch, names = await self._async_batch(n)
            lookups = self._lookups(batch, names)

            async def run() -> None:
                lookups()

            samples.append(await self._sample_async(run))
            self.assertEqual(helper.mock_calls, [], "get_member() sent requests")
        self._assert_flat("AsyncSandboxBatch.get_member (live and missing)", *samples)

    def test_sync_err_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            helper, batch, _names = self._sync_batch(n)
            samples.append(self._sample_sync(lambda: self.assertIsNone(batch.err())))
            self.assertEqual(helper.method_calls, [], "err() sent requests")
        self._assert_flat("SandboxBatch.err", *samples)

    async def test_async_err_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            helper, batch, _names = await self._async_batch(n)

            async def run() -> None:
                self.assertIsNone(batch.err())

            samples.append(await self._sample_async(run))
            self.assertEqual(helper.mock_calls, [], "err() sent requests")
        self._assert_flat("AsyncSandboxBatch.err", *samples)


def _ready_on_create(batch: Any) -> Callable[..., None]:
    """Makes each create land Ready in the batch state, as the watch would report it."""

    def ready(claim_name: str, warmpool: str, *_a: object, **_kw: object) -> None:
        obj = _ready_claim("req", 0, warmpool, 0)
        obj["metadata"] = {
            "name": claim_name, "uid": f"uid-{claim_name}", "resourceVersion": "5"
        }
        batch._state.upsert_claim(obj)

    return ready


class TestSuiteAEpisodeHotPaths(_HandleGateBase):
    """Suite A: the two handle calls RL batch mode makes per episode, a warm-covered
    ``acquire()`` and ``release_member()``, stay flat as the batch, its connection cache, and
    the number of other waiting acquires grow, and a release wakes at most one waiter."""

    # A warm-covered acquire skips pacing; an uncovered one reserves a pacing slot. Pacing is
    # set to no delay, so the uncovered path's own work is measured without sleeping.
    COVERAGE = (("warm-covered", 10 * 3), ("paced", 0))

    def test_sync_acquire_is_o1(self) -> None:
        for label, credit in self.COVERAGE:
            samples = []
            for n in self.SIZES:
                helper, batch, _names = self._sync_batch(n)
                ready = _ready_on_create(batch)

                def ready_locked(*args: Any, **kwargs: Any) -> None:
                    with batch._lock:
                        ready(*args, **kwargs)

                helper.create_sandbox_claim.side_effect = ready_locked
                batch._warm_credit.record("pool-a", credit)
                batch._create_interval = 0.0
                self._waiters(batch, n, _CountingEvent)
                _CountingEvent.sets = 0
                samples.append(self._sample_sync(lambda: batch.acquire("pool-a", timeout=5)))
                self.assertEqual(_CountingEvent.sets, 0, "an acquire woke other acquires")
                self.assertEqual(
                    [name for name, _a, _k in helper.method_calls],
                    ["create_sandbox_claim"] * (1 + 2 * self.REPEATS),
                    f"a {label} acquire sent requests besides its create",
                )
            with self.subTest(coverage=label):
                self._assert_flat(f"SandboxBatch.acquire ({label})", *samples)

    async def test_async_acquire_is_o1(self) -> None:
        for label, credit in self.COVERAGE:
            samples = []
            for n in self.SIZES:
                helper, batch, _names = await self._async_batch(n)
                helper.create_sandbox_claim = AsyncMock(side_effect=_ready_on_create(batch))
                batch._warm_credit.record("pool-a", credit)
                batch._create_interval = 0.0
                self._waiters(batch, n, _CountingAsyncEvent)
                _CountingAsyncEvent.sets = 0

                async def run() -> None:
                    await batch.acquire("pool-a", timeout=5)

                samples.append(await self._sample_async(run))
                self.assertEqual(_CountingAsyncEvent.sets, 0, "an acquire woke other acquires")
                self.assertEqual(
                    helper.create_sandbox_claim.await_count, 1 + 2 * self.REPEATS,
                    f"a {label} acquire sent a create count other than one",
                )
            with self.subTest(coverage=label):
                self._assert_flat(f"AsyncSandboxBatch.acquire ({label})", *samples)

    POOLS = (10, 2000)

    def _known_pools(self, batch: Any, p: int) -> None:
        _seed_known_pools(batch, p)
        # A rename must not silently stop the walk counting.
        self.assertIsInstance(batch._warm_credit._credit, _WalkCountingDict)
        _WalkCountingDict.walks = 0

    def _assert_no_pool_walks(self, label: str) -> None:
        self.assertEqual(
            _WalkCountingDict.walks, 0,
            f"{label}: walked a whole per-pool table {_WalkCountingDict.walks} times "
            f"(O(P) per acquire)",
        )

    def test_sync_acquire_is_o1_in_known_pool_count(self) -> None:
        samples = []
        for p in self.POOLS:
            helper, batch, _names = self._sync_batch(10)
            self._known_pools(batch, p)
            ready = _ready_on_create(batch)

            def ready_locked(*args: Any, **kwargs: Any) -> None:
                with batch._lock:
                    ready(*args, **kwargs)

            helper.create_sandbox_claim.side_effect = ready_locked
            batch._warm_credit.record("pool-a", 10 * self.REPEATS)
            _WalkCountingDict.walks = 0
            samples.append(self._sample_sync(lambda: batch.acquire("pool-a", timeout=5)))
            self._assert_no_pool_walks(f"SandboxBatch.acquire at P={p}")
        sizes = f"P={self.POOLS[0]}|P={self.POOLS[1]}"
        assert_scaling(self, "SandboxBatch.acquire vs pools", samples[0][0], samples[1][0],
                       O1_MAX_RATIO, sizes)
        assert_peak_scaling(self, "SandboxBatch.acquire vs pools", samples[0][1], samples[1][1],
                            sizes)

    async def test_async_acquire_is_o1_in_known_pool_count(self) -> None:
        samples = []
        for p in self.POOLS:
            helper, batch, _names = await self._async_batch(10)
            self._known_pools(batch, p)
            helper.create_sandbox_claim = AsyncMock(side_effect=_ready_on_create(batch))
            batch._warm_credit.record("pool-a", 10 * self.REPEATS)

            async def run() -> None:
                await batch.acquire("pool-a", timeout=5)

            _WalkCountingDict.walks = 0
            samples.append(await self._sample_async(run))
            self._assert_no_pool_walks(f"AsyncSandboxBatch.acquire at P={p}")
        sizes = f"P={self.POOLS[0]}|P={self.POOLS[1]}"
        assert_scaling(self, "AsyncSandboxBatch.acquire vs pools", samples[0][0], samples[1][0],
                       O1_MAX_RATIO, sizes)
        assert_peak_scaling(self, "AsyncSandboxBatch.acquire vs pools", samples[0][1],
                            samples[1][1], sizes)

    def test_sync_release_member_is_o1_and_wakes_no_other_waiter(self) -> None:
        samples = []
        for n in self.SIZES:
            _helper, batch, names = self._sync_batch(n)
            self._waiters(batch, n, _CountingEvent)
            members = [batch._state.get_member(name) for name in names]
            _CountingEvent.sets = 0

            def run() -> None:
                member = members.pop()
                assert member is not None
                batch.release_member(member)
                batch._wait_active_deletes()

            released = len(members)
            samples.append(self._sample_sync(run))
            released -= len(members)
            self.assertGreater(released, 0)
            # No parked waiter belongs to a released member, so none may wake.
            self.assertEqual(_CountingEvent.sets, 0, "release woke other waiters")
        self._assert_flat("SandboxBatch.release_member", *samples)

    async def test_async_release_member_is_o1_and_wakes_no_other_waiter(self) -> None:
        samples = []
        for n in self.SIZES:
            _helper, batch, names = await self._async_batch(n)
            self._waiters(batch, n, _CountingAsyncEvent)
            members = [batch._state.get_member(name) for name in names]
            _CountingAsyncEvent.sets = 0

            async def run() -> None:
                member = members.pop()
                assert member is not None
                await batch.release_member(member)
                await batch._wait_active_deletes()

            released = len(members)
            samples.append(await self._sample_async(run))
            released -= len(members)
            self.assertGreater(released, 0)
            self.assertEqual(_CountingAsyncEvent.sets, 0, "release woke other waiters")
        self._assert_flat("AsyncSandboxBatch.release_member", *samples)


FILL_GROUP_SIZE = 5


def _fill_groups(n: int) -> list[BatchGroup]:
    """``n // 5`` groups of 5, so a fill of ``n`` creates also grows the group count."""
    return [BatchGroup(warmpool=f"pool-{i}", size=FILL_GROUP_SIZE) for i in range(n // 5)]


def _credit_every_group(batch: Any, credit: int = 10**6) -> None:
    """Sets every group's warm credit: plenty, so creates skip pacing, or none, so every create
    reserves a pacing slot (with pacing set to no delay, so nothing sleeps)."""
    for group in batch._state.groups:
        batch._warm_credit.record(group.warmpool, credit)
    batch._create_interval = 0.0


FILL_COVERAGE = (("warm", 10**6), ("cold", 0))


class TestSuiteAFillAndConnectMiss(_HandleGateBase):
    """Suite A: one fill create and one ``connect()`` cache miss (RL batch mode connects a fresh
    member every episode) stay flat as the batch and its connection cache grow, and send only
    the create itself or nothing. A fill create is measured mid-fill in a batch of ``n // 5``
    groups: half the plan has landed Ready and connected, and other acquires are in flight."""

    def _land_half_sync(self, batch: Any, helper: Any, n: int) -> None:
        """Sends the first half of the plan through the real worker and lands each create
        Ready and connected, as the watch would mid-fill, so a create's cost can't hide a walk
        of the landed members or their connections. Also puts ``n`` acquires in flight."""
        landed = 0

        def land(claim_name: str, warmpool: str, *_a: object, **_k: object) -> None:
            nonlocal landed
            obj = _ready_claim(batch.batch_id, landed, warmpool, FILL_GROUP_SIZE)
            obj["metadata"]["name"] = claim_name
            batch._state.upsert_claim(obj)
            landed += 1
            if landed >= n // 2:
                batch._stop_creating.set()

        helper.create_sandbox_claim.side_effect = land
        _credit_every_group(batch)
        batch._create_worker()
        self.assertEqual(landed, n // 2)
        for member in batch._state.members():
            if member.state is MemberState.READY:
                batch.connect(member)
        self.assertGreaterEqual(len(batch._connected), n // 2)
        self._waiters(batch, n, _CountingEvent)
        _seed_known_pools(batch, n)
        _count_walks_of_large_tables([batch, batch._state], n // FILL_GROUP_SIZE)

    async def _land_half_async(self, batch: Any, helper: Any, n: int) -> None:
        landed = 0

        async def land(claim_name: str, warmpool: str, *_a: object, **_k: object) -> None:
            nonlocal landed
            obj = _ready_claim(batch.batch_id, landed, warmpool, FILL_GROUP_SIZE)
            obj["metadata"]["name"] = claim_name
            batch._state.upsert_claim(obj)
            landed += 1
            if landed >= n // 2:
                batch._stop_creating.set()

        helper.create_sandbox_claim = land
        _credit_every_group(batch)
        await batch._create_worker()
        self.assertEqual(landed, n // 2)
        for member in batch._state.members():
            if member.state is MemberState.READY:
                await batch.connect(member)
        self.assertGreaterEqual(len(batch._connected), n // 2)
        self._waiters(batch, n, _CountingAsyncEvent)
        _seed_known_pools(batch, n)
        _count_walks_of_large_tables([batch, batch._state], n // FILL_GROUP_SIZE)

    def test_sync_fill_create_is_o1(self) -> None:
        for label, credit in FILL_COVERAGE:
            samples = []
            for n in self.SIZES:
                # claim_batch plans the groups' n fill creates (its worker is stubbed until
                # now), so per-create work can't hide a scan of the plan; half have landed.
                _client, helper, batch = _new_stopped_sync_batch(
                    _fill_groups(n), batch_id="b-fill"
                )
                self._land_half_sync(batch, helper, n)
                batch._stop_creating.clear()
                helper.reset_mock()
                _credit_every_group(batch, credit)
                # Stop the worker after each create, so every run does exactly one.
                helper.create_sandbox_claim.side_effect = (
                    lambda *_a, **_k: batch._stop_creating.set()
                )

                def run() -> None:
                    batch._stop_creating.clear()
                    batch._create_worker()

                samples.append(self._sample_sync(run))
                self.assertEqual(
                    [name for name, _a, _k in helper.method_calls],
                    ["create_sandbox_claim"] * (1 + 2 * self.REPEATS),
                )
            with self.subTest(fill=label):
                self._assert_flat(f"SandboxBatch._create_worker (1 {label} fill create)",
                                  *samples)

    async def test_async_fill_create_is_o1(self) -> None:
        for label, credit in FILL_COVERAGE:
            samples = []
            for n in self.SIZES:
                _client, helper, batch = await _new_stopped_async_batch(
                    _fill_groups(n), batch_id="b-fill"
                )
                await self._land_half_async(batch, helper, n)
                batch._stop_creating.clear()
                helper.reset_mock()
                helper.create_sandbox_claim = AsyncMock(
                    side_effect=lambda *_a, **_k: batch._stop_creating.set()
                )
                _credit_every_group(batch, credit)

                async def run() -> None:
                    batch._stop_creating.clear()
                    await batch._create_worker()

                samples.append(await self._sample_async(run))
                self.assertEqual(helper.create_sandbox_claim.await_count, 1 + 2 * self.REPEATS)
                self.assertEqual(
                    [str(c).split("(")[0] for c in helper.mock_calls],
                    ["call.create_sandbox_claim"] * (1 + 2 * self.REPEATS),
                )
            with self.subTest(fill=label):
                self._assert_flat(
                    f"AsyncSandboxBatch._create_worker (1 {label} fill create)", *samples
                )

    def test_sync_connect_cache_miss_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            helper, batch, names = self._sync_batch(n)
            member = batch._state.get_member(names[n // 2])
            assert member is not None
            miss = member

            def run() -> None:
                batch._connected.pop(miss.claim_name, None)
                batch.connect(miss)

            samples.append(self._sample_sync(run))
            self.assertEqual(helper.method_calls, [], "connect() cache miss sent requests")
        self._assert_flat("SandboxBatch.connect (cache miss)", *samples)

    async def test_async_connect_cache_miss_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            helper, batch, names = await self._async_batch(n)
            member = batch._state.get_member(names[n // 2])
            assert member is not None
            miss = member

            async def run() -> None:
                batch._connected.pop(miss.claim_name, None)
                await batch.connect(miss)

            samples.append(await self._sample_async(run))
            self.assertEqual(helper.mock_calls, [], "connect() cache miss sent requests")
        self._assert_flat("AsyncSandboxBatch.connect (cache miss)", *samples)


class TestSuiteAConsumerIteration(_HandleGateBase):
    """Suite A: handing out one event through ``events()`` (``_next``) stays flat as the batch
    grows, and so does the done check once the queue is drained."""

    def _queue_events(self, batch: Any, n: int) -> None:
        for idx in range(n):
            batch._state.upsert_claim(_ready_claim(batch.batch_id, idx, "pool-a", n))
        batch._state.set_mode(ConsumerMode.STREAM)
        _seed_known_pools(batch, n)
        _count_walks_of_large_tables([batch, batch._state], n // 5)

    def test_sync_next_event_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            _client, _helper, batch = _new_stopped_sync_batch(
                [BatchGroup(warmpool="pool-a", size=n)] + _groups_for(n)[1:], batch_id="b-iter"
            )
            self._queue_events(batch, n)
            state = batch._state
            samples.append(
                self._sample_sync(lambda: batch._next(state.pop_event, state.events_done))
            )
        self._assert_flat("SandboxBatch._next (one events() item)", *samples)

    async def test_async_next_event_is_o1(self) -> None:
        samples = []
        for n in self.SIZES:
            _client, _helper, batch = await _new_stopped_async_batch(
                [BatchGroup(warmpool="pool-a", size=n)] + _groups_for(n)[1:], batch_id="b-iter"
            )
            self._queue_events(batch, n)
            state = batch._state

            async def run() -> None:
                await batch._next(state.pop_event, state.events_done)

            samples.append(await self._sample_async(run))
        self._assert_flat("AsyncSandboxBatch._next (one events() item)", *samples)


_HANDLE_CHURN_SCRIPT = """
import asyncio, gc, logging, sys, tracemalloc
from k8s_agent_sandbox.test.unit.test_batch_fault_injection import (
    _new_stopped_async_batch, _new_stopped_sync_batch)
from k8s_agent_sandbox.test.unit.test_batch_performance_invariants import (
    _ready_claim, _ready_on_create)

from k8s_agent_sandbox import async_sandbox_batch, sandbox_batch
from k8s_agent_sandbox.async_sandbox import AsyncSandbox
from k8s_agent_sandbox.sandbox import Sandbox

WARMUP, EPISODES = int(sys.argv[2]), int(sys.argv[3])
logging.disable(logging.INFO)
# A constant lifecycle spec: formatting a fresh shutdownTime per create leaves a varying number
# of short strings live in CPython for a while (no Python object references them), which would
# be noise here. The spec's content doesn't affect what the handle retains.
_LIFECYCLE = {"shutdownTime": "2030-01-01T00:00:00Z", "shutdownPolicy": "Delete"}
for module in (sandbox_batch, async_sandbox_batch):
    module.construct_sandbox_claim_lifecycle_spec = lambda _seconds: dict(_LIFECYCLE)


def watch_events(name, rv):
    # The watch's view of one episode: the claim turns Ready, then is deleted.
    obj = _ready_claim("churn", 0, "pool-a", 0)
    obj["metadata"] = {"name": name, "uid": "uid-" + name, "resourceVersion": str(rv)}
    gone = dict(obj, metadata=dict(obj["metadata"], resourceVersion=str(rv + 1)))
    return [{"type": "MODIFIED", "object": obj}, {"type": "DELETED", "object": gone}]


class PlainHelper:
    # Only the requests an episode needs. The real Sandbox that connect() builds keeps this
    # helper, and a MagicMock there would record every truth test and attribute call.
    def __init__(self, **requests):
        # The connector reads this to pick its API client (None: the default configuration).
        self.injected_api_client = None
        self.__dict__.update(requests)


def measure(run_episodes, batch, helper):
    # Trace from before the warmup, so objects the measured episodes replace (each member's
    # latest state, for example) are traced too, and freeing them offsets their replacements.
    tracemalloc.start()
    run_episodes(WARMUP)
    # Measure both points without F-35's tombstones, which are pinned separately.
    batch._state._released.clear()
    gc.collect()
    before = tracemalloc.get_traced_memory()[0]
    # Every helper call an episode needs is a plain function, so any call a mock helper records
    # is an extra request, which must be reported as one rather than as the mock's retained
    # memory. A PlainHelper has no mock record: any other request raises AttributeError.
    calls_before = len(getattr(helper, "method_calls", []))
    run_episodes(EPISODES)
    extra = getattr(helper, "method_calls", [])[calls_before:]
    if extra:
        sys.exit(f"episodes sent {len(extra)} unexpected helper calls, e.g. {extra[:3]}")
    batch._state._released.clear()
    gc.collect()
    print((tracemalloc.get_traced_memory()[0] - before) / EPISODES)


def sync_main():
    client, helper, batch = _new_stopped_sync_batch(batch_id="b-churn")
    # The real Sandbox class, so connect()'s own costs count; it talks to the cluster only
    # through the helper, whose calls measure() checks.
    client.sandbox_class = Sandbox
    ready = _ready_on_create(batch)

    def create(*args, **kwargs):
        with batch._lock:
            ready(*args, **kwargs)

    helper.create_sandbox_claim = create
    helper.delete_sandbox_claim = lambda *_a, **_k: None
    batch._warm_credit.record("pool-a", 10**9)
    pending = []

    # A plain function, since a MagicMock records every call.
    def watch(*_a, **_k):
        yield from pending
        batch._watch_stop.set()

    helper.watch_sandbox_claims = watch
    helper = client.k8s_helper = PlainHelper(
        create_sandbox_claim=create, delete_sandbox_claim=helper.delete_sandbox_claim,
        watch_sandbox_claims=watch)
    rv = [10]

    def run(count):
        for _ in range(count):
            member = batch.acquire("pool-a", timeout=5)
            # RL batch mode connects every acquired member.
            batch.connect(member)
            batch.release_member(member)
            batch._wait_active_deletes()
            # Deliver the episode's watch events through the handle's watch loop too.
            rv[0] += 2
            pending[:] = watch_events(member.claim_name, rv[0])
            batch._watch_stop.clear()
            batch._watch_loop()

    measure(run, batch, helper)


async def async_main():
    client, helper, batch = await _new_stopped_async_batch(batch_id="b-churn")
    client.sandbox_class = AsyncSandbox
    ready = _ready_on_create(batch)

    async def create(*args, **kwargs):
        ready(*args, **kwargs)
        # A real create suspends on network I/O, which lets the loop purge cancelled timers.
        await asyncio.sleep(0)

    async def delete(*_a, **_k):
        await asyncio.sleep(0)

    helper.create_sandbox_claim = create
    helper.delete_sandbox_claim = delete
    batch._warm_credit.record("pool-a", 10**9)
    loop = asyncio.get_running_loop()
    pending = []

    async def watch(*_a, **_k):
        for event in pending:
            yield event
        # The loop re-raises cancellation before its error handling, so the batch stays healthy.
        raise asyncio.CancelledError()

    helper.watch_sandbox_claims = watch
    helper = client.k8s_helper = PlainHelper(
        create_sandbox_claim=create, delete_sandbox_claim=delete, watch_sandbox_claims=watch)
    rv = [10]

    async def episodes(count):
        for _ in range(count):
            member = await batch.acquire("pool-a", timeout=5)
            await batch.connect(member)
            await batch.release_member(member)
            await batch._wait_active_deletes()
            rv[0] += 2
            pending[:] = watch_events(member.claim_name, rv[0])
            try:
                await batch._watch_loop()
            except asyncio.CancelledError:
                pass
        assert batch._state.error() is None

    def run(count):
        # Run on the same loop from a worker thread so measure() stays synchronous.
        asyncio.run_coroutine_threadsafe(episodes(count), loop).result()

    await loop.run_in_executor(None, measure, run, batch, helper)


def modified_events(batch, count, start_rv):
    # MODIFIED events with fresh resourceVersions for existing members, as a watch delivers
    # every status heartbeat or readiness flap of a long-lived batch.
    names = [m.claim_name for m in batch._state.members("pool-a")]
    events = []
    for i in range(count):
        name = names[i % len(names)]
        obj = _ready_claim("churn", 0, "pool-a", 0)
        obj["metadata"] = {"name": name, "uid": "uid-" + name, "resourceVersion": str(start_rv + i)}
        events.append({"type": "MODIFIED", "object": obj})
    return events


def sync_watch_main():
    _client, helper, batch = _new_stopped_sync_batch(batch_id="b-churn")
    ready = _ready_on_create(batch)
    helper.create_sandbox_claim = lambda *a, **k: ready(*a, **k)
    batch._warm_credit.record("pool-a", 100)
    for _ in range(50):
        batch.acquire("pool-a", timeout=5)
    pending = []

    def watch(*_a, **_k):
        yield from pending
        batch._watch_stop.set()

    helper.watch_sandbox_claims = watch
    rv = [1000]

    def run(count):
        pending[:] = modified_events(batch, count, rv[0])
        rv[0] += count
        batch._watch_stop.clear()
        batch._watch_loop()
        # Drop the test's own copies of the delivered events before measuring.
        pending.clear()

    measure(run, batch, helper)


async def async_watch_main():
    _client, helper, batch = await _new_stopped_async_batch(batch_id="b-churn")
    ready = _ready_on_create(batch)

    async def create(*args, **kwargs):
        ready(*args, **kwargs)

    helper.create_sandbox_claim = create
    batch._warm_credit.record("pool-a", 100)
    for _ in range(50):
        await batch.acquire("pool-a", timeout=5)
    loop = asyncio.get_running_loop()
    pending = []

    async def watch(*_a, **_k):
        for event in pending:
            yield event
        raise asyncio.CancelledError()

    helper.watch_sandbox_claims = watch
    rv = [1000]

    async def stream(count):
        pending[:] = modified_events(batch, count, rv[0])
        rv[0] += count
        try:
            await batch._watch_loop()
        except asyncio.CancelledError:
            pass
        pending.clear()
        assert batch._state.error() is None

    def run(count):
        asyncio.run_coroutine_threadsafe(stream(count), loop).result()

    await loop.run_in_executor(None, measure, run, batch, helper)


mode = sys.argv[1]
if mode == "sync":
    sync_main()
elif mode == "async":
    asyncio.run(async_main())
elif mode == "sync-watch":
    sync_watch_main()
else:
    asyncio.run(async_watch_main())
"""


class TestSuiteAWatchEventKinds(_HandleGateBase):
    """Suite A: every kind of watch event costs the same at any batch, connection-cache, and
    in-flight-acquire size, wakes no unrelated acquire, sends no request beyond the stubbed watch
    stream, and walks no whole per-member table: a status heartbeat (MODIFIED with a new
    resourceVersion), a new claim (ADDED), a Ready flap, a pod IP change (which closes the
    member's cached connection), and a deletion (one per RL episode, after release_member()).

    Each sample gets a fresh batch, since most of these change the batch only the first time.
    """

    KINDS = ("HEARTBEAT", "ADDED", "FLAP", "IP_CHANGE", "DELETED")

    def _event(self, batch: Any, names: list[str], kind: str, n: int) -> dict[str, Any]:
        if kind == "ADDED":
            obj = _ready_claim("seed", 0, "pool-a", 0)
            obj["metadata"] = {"name": "b-hot-new", "uid": "uid-new", "resourceVersion": "7"}
            return {"type": "ADDED", "object": obj}
        event = _modified_event(batch._state, names[n // 2])
        event["object"]["metadata"]["resourceVersion"] = "999999"
        if kind == "FLAP":
            event["object"]["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
        elif kind == "IP_CHANGE":
            event["object"]["status"]["sandbox"]["podIPs"] = ["10.255.0.1"]
        elif kind == "DELETED":
            return {"type": "DELETED", "object": event["object"]}
        return event

    def _assert_applied(self, batch: Any, kind: str, name: str) -> None:
        """Fails a sample whose event changed nothing, which would measure a no-op."""
        member = batch._state.get_member(name)
        if kind == "ADDED":
            self.assertIsNotNone(batch._state.get_member("b-hot-new"))
        elif kind == "FLAP":
            assert member is not None
            self.assertIsNot(member.state, MemberState.READY)
        elif kind in ("IP_CHANGE", "DELETED"):
            self.assertNotIn(name, batch._connected, f"{kind} kept the stale connection")
        if kind == "DELETED":
            self.assertTrue(member is None or member.state is not MemberState.READY)

    def _prepare(self, batch: Any, n: int, cls: type) -> None:
        self._waiters(batch, n, cls)
        # A rename must not silently stop the walk counting.
        self.assertIsInstance(batch._connected, _WalkCountingDict)

    def _assert_clean(self, kind: str, sets: int) -> None:
        self.assertEqual(sets, 0, f"a {kind} event woke unrelated acquires")
        self.assertEqual(
            _WalkCountingDict.walks, 0,
            f"a {kind} event walked a whole per-member table {_WalkCountingDict.walks} times",
        )

    def test_sync_watch_event_kinds_are_o1(self) -> None:
        for kind in self.KINDS:
            samples = []
            for n in self.SIZES:
                _CountingEvent.sets = 0

                def build() -> Callable[[], object]:
                    helper, batch, names = self._sync_batch(n)
                    event = self._event(batch, names, kind, n)
                    self._prepare(batch, n, _CountingEvent)
                    _WalkCountingDict.walks = 0

                    def watch(*_a: object, **_kw: object) -> Iterator[dict[str, Any]]:
                        yield event
                        batch._watch_stop.set()

                    helper.watch_sandbox_claims.side_effect = watch

                    def run() -> None:
                        batch._watch_loop()
                        self._assert_applied(batch, kind, names[n // 2])
                        self.assertEqual(
                            [name for name, _a, _k in helper.method_calls],
                            ["watch_sandbox_claims"], f"a {kind} event sent requests",
                        )
                        self._assert_clean(kind, _CountingEvent.sets)

                    return run

                samples.append(self._sample_fresh_sync(build))
            with self.subTest(kind=kind):
                self._assert_flat(f"SandboxBatch._watch_loop (1 {kind} event)", *samples)

    async def test_async_watch_event_kinds_are_o1(self) -> None:
        for kind in self.KINDS:
            samples = []
            for n in self.SIZES:
                _CountingAsyncEvent.sets = 0

                async def build() -> Callable[[], Awaitable[object]]:
                    helper, batch, names = await self._async_batch(n)
                    event = self._event(batch, names, kind, n)
                    self._prepare(batch, n, _CountingAsyncEvent)
                    _WalkCountingDict.walks = 0

                    async def watch(*_a: object, **_kw: object) -> AsyncIterator[dict[str, Any]]:
                        yield event
                        # The loop re-raises cancellation before any error handling, so the
                        # batch stays healthy and only the event's own work is measured.
                        raise asyncio.CancelledError()

                    helper.watch_sandbox_claims = watch

                    async def run() -> None:
                        with contextlib.suppress(asyncio.CancelledError):
                            await batch._watch_loop()
                        self.assertIsNone(batch._state.error())
                        self._assert_applied(batch, kind, names[n // 2])
                        self.assertEqual(helper.mock_calls, [], f"a {kind} event sent requests")
                        self._assert_clean(kind, _CountingAsyncEvent.sets)

                    return run

                samples.append(await self._sample_fresh_async(build))
            with self.subTest(kind=kind):
                self._assert_flat(f"AsyncSandboxBatch._watch_loop (1 {kind} event)", *samples)


class TestSuiteAHandleReleaseGroup(_HandleGateBase):
    """Suite A: the handle's release_group() during the fill drops one pool's planned creates
    in O(K_pool), not by walking every pool's plan (F-36), and wakes only that pool's waiting
    acquires (F-37)."""

    POOLS = (20, 1000)
    PER_POOL = 5

    def _assert_drop_pending(self, batch: Any) -> None:
        """Each sample's batch is fresh, so its release_group has the pool's whole planned
        share to drop, and the counted run does the drop rather than a no-op repeat."""
        planned = [g.warmpool for _name, g in batch._create_plan].count("pool-1")
        self.assertEqual(planned, self.PER_POOL)
        self.assertEqual(batch._state.pool_release_seq("pool-1"), 0)

    def test_sync_release_group_is_o1_in_planned_pools(self) -> None:
        samples = []
        for g in self.POOLS:
            groups = [BatchGroup(warmpool=f"pool-{i}", size=self.PER_POOL) for i in range(g)]

            def build() -> Callable[[], object]:
                _client, _helper, batch = _new_stopped_sync_batch(groups, batch_id="b-rg")
                self._assert_drop_pending(batch)
                _count_walks_of_large_tables([batch, batch._state, batch._warm_credit], g)

                def run() -> None:
                    batch.release_group("pool-1")
                    self.assertEqual(batch._state.pool_release_seq("pool-1"), 1)

                return run

            samples.append(self._sample_fresh_sync(build))
        sizes = f"G={self.POOLS[0]}x{self.PER_POOL} planned|G={self.POOLS[1]}x{self.PER_POOL}"
        assert_scaling(self, "SandboxBatch.release_group", samples[0][0], samples[1][0],
                       PER_POOL_MAX_RATIO, sizes)
        assert_peak_scaling(self, "SandboxBatch.release_group", samples[0][1], samples[1][1],
                            sizes)
        self._assert_no_walks("SandboxBatch.release_group")

    async def test_async_release_group_is_o1_in_planned_pools(self) -> None:
        samples = []
        for g in self.POOLS:
            groups = [BatchGroup(warmpool=f"pool-{i}", size=self.PER_POOL) for i in range(g)]

            async def build() -> Callable[[], Awaitable[object]]:
                _client, _helper, batch = await _new_stopped_async_batch(groups, batch_id="b-rg")
                self._assert_drop_pending(batch)
                _count_walks_of_large_tables([batch, batch._state, batch._warm_credit], g)

                async def run() -> None:
                    await batch.release_group("pool-1")
                    self.assertEqual(batch._state.pool_release_seq("pool-1"), 1)

                return run

            samples.append(await self._sample_fresh_async(build))
        sizes = f"G={self.POOLS[0]}x{self.PER_POOL} planned|G={self.POOLS[1]}x{self.PER_POOL}"
        assert_scaling(self, "AsyncSandboxBatch.release_group", samples[0][0], samples[1][0],
                       PER_POOL_MAX_RATIO, sizes)
        assert_peak_scaling(self, "AsyncSandboxBatch.release_group", samples[0][1],
                            samples[1][1], sizes)
        self._assert_no_walks("AsyncSandboxBatch.release_group")

    def _other_pool_waiters(self, batch: Any, w: int, cls: type) -> None:
        for _ in range(w):
            name = batch._state.start_acquire("pool-b")
            batch._acquire_waiters[name] = cls()

    def test_sync_release_group_wakes_only_its_pool(self) -> None:
        _client, _helper, batch = _new_stopped_sync_batch(
            [BatchGroup(warmpool="pool-a", size=1), BatchGroup(warmpool="pool-b", size=0)],
            batch_id="b-wake",
        )
        self._other_pool_waiters(batch, 100, _CountingEvent)
        _CountingEvent.sets = 0
        batch.release_group("pool-a")
        self.assertEqual(_CountingEvent.sets, 0, "release_group woke another pool's acquires")

    async def test_async_release_group_wakes_only_its_pool(self) -> None:
        _client, _helper, batch = await _new_stopped_async_batch(
            [BatchGroup(warmpool="pool-a", size=1), BatchGroup(warmpool="pool-b", size=0)],
            batch_id="b-wake",
        )
        self._other_pool_waiters(batch, 100, _CountingAsyncEvent)
        _CountingAsyncEvent.sets = 0
        await batch.release_group("pool-a")
        self.assertEqual(
            _CountingAsyncEvent.sets, 0, "release_group woke another pool's acquires"
        )


class TestSuiteCHandleChurn(unittest.TestCase):
    """Suite C: a long-lived handle keeps nothing per acquire and release_member episode beyond
    the batch state's released-name tombstones (F-35).

    Runs in a fresh interpreter, since a test runner's own bookkeeping (pytest retains objects
    across a test's frames) would otherwise be counted as the handle's.
    """

    WARMUP = 500
    EPISODES = 3000

    def _retained_per_episode(self, mode: str) -> float:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(pathlib.Path(__file__).resolve().parents[3]), env.get("PYTHONPATH", "")]
        )
        proc = subprocess.run(
            [sys.executable, "-c", _HANDLE_CHURN_SCRIPT, mode, str(self.WARMUP),
             str(self.EPISODES)],
            capture_output=True, text=True, env=env, timeout=300, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        return float(proc.stdout.strip().splitlines()[-1])

    def test_sync_handle_episodes_retain_no_memory(self) -> None:
        per_episode = self._retained_per_episode("sync")
        self.assertLess(per_episode, 4, f"{per_episode:.1f} B/episode retained by the handle")

    def test_async_handle_episodes_retain_no_memory(self) -> None:
        per_episode = self._retained_per_episode("async")
        self.assertLess(per_episode, 4, f"{per_episode:.1f} B/episode retained by the handle")

    def test_sync_watch_events_retain_no_memory(self) -> None:
        # A long-lived batch sees a MODIFIED event per member heartbeat or flap; the handle
        # and its state must keep nothing per event.
        per_event = self._retained_per_episode("sync-watch")
        self.assertLess(per_event, 4, f"{per_event:.1f} B/event retained by the watch loop")

    def test_async_watch_events_retain_no_memory(self) -> None:
        per_event = self._retained_per_episode("async-watch")
        self.assertLess(per_event, 4, f"{per_event:.1f} B/event retained by the watch loop")


class _CountingConnection:
    """A stand-in for a cached ``Sandbox`` connection that counts equality comparisons, which
    ``list`` membership and ``list.remove`` run in C without executing Python bytecode."""

    eq_calls = 0

    def __eq__(self, other: object) -> bool:
        type(self).eq_calls += 1
        return self is other

    __hash__ = object.__hash__


def _stale_backlog(size: int) -> tuple[list[_CountingConnection], list[_CountingConnection]]:
    """Returns ``(stuck, closing)``: ``stuck`` connections an earlier close failed to close sit
    ahead of the ``closing`` ones in ``_stale_connections``."""
    return (
        [_CountingConnection() for _ in range(size)],
        [_CountingConnection() for _ in range(size)],
    )


class TestSuiteAStaleConnectionScans(unittest.IsolatedAsyncioTestCase):
    """Suite A: closing S stale connections must cost O(S) comparisons, not O(S^2) (F-32).

    ``Sandbox`` inherits identity ``__eq__``, so production ``list`` scans compare pointers in
    C, and the common case (closing exactly the connections just appended) hits index 0. The
    quadratic case needs a backlog: connections whose earlier close failed stay at the front of
    ``_stale_connections``, and every later lookup walks past all of them under the batch lock.
    The stand-in below counts those comparisons through a Python ``__eq__``.
    """

    SIZES = (50, 800)
    instructions = 0

    async def _eq_calls(self, size: int, *, use_async: bool) -> int:
        stuck, closing = _stale_backlog(size)
        closed: list[object] = []
        if use_async:
            _client, _helper, abatch = await _new_stopped_async_batch(batch_id="b-f32")
            batch_any: Any = abatch
            container = _container_like(abatch._stale_connections, stuck + closing)
            batch_any._stale_connections = container

            async def close_one(_self: object, sandbox: object, *_a: object) -> None:
                closed.append(sandbox)

            _CountingConnection.eq_calls = 0
            with patch.object(AsyncSandboxBatch, "_close_one", close_one):
                with InstructionCounter() as counter:
                    await abatch._close_connections(closing, "re-list")  # type: ignore[arg-type]
        else:
            _sclient, _shelper, sbatch = _new_stopped_sync_batch(batch_id="b-f32")
            batch_any = sbatch
            container = _container_like(sbatch._stale_connections, stuck + closing)
            batch_any._stale_connections = container

            def sync_close_one(_self: object, sandbox: object, *_a: object) -> None:
                closed.append(sandbox)

            _CountingConnection.eq_calls = 0
            with patch.object(SandboxBatch, "_close_one", sync_close_one):
                with InstructionCounter() as counter:
                    sbatch._close_connections(closing, "re-list")  # type: ignore[arg-type]
        eq_calls = _CountingConnection.eq_calls
        self.instructions = counter.count
        # A container change must still close every connection it was asked to close, and keep
        # the backlog for the next cleanup, or a "fix" could pass by skipping the work.
        self.assertEqual(closed, closing)
        self.assertCountEqual(list(container), stuck)
        return eq_calls

    def _assert_linear(self, label: str, small: int, large: int) -> None:
        s_small, s_large = self.SIZES
        # A linear close compares each connection a bounded number of times; allow 4 per entry.
        self.assertLessEqual(
            large,
            4 * 2 * s_large,
            f"{label}: {small:,} comparisons closing S={s_small} behind {s_small} stuck vs "
            f"{large:,} at S={s_large} (O(S^2) list membership + remove under the batch lock)",
        )

    async def _check(self, label: str, use_async: bool) -> None:
        small = await self._eq_calls(self.SIZES[0], use_async=use_async)
        small_instructions = self.instructions
        large = await self._eq_calls(self.SIZES[1], use_async=use_async)
        large_instructions = self.instructions
        self._assert_linear(label, small, large)
        # A quadratic close written in Python (identity scans, a rebuild per item) executes
        # O(S^2) instructions instead of calling __eq__.
        s_small, s_large = self.SIZES
        assert_scaling(
            self, label, small_instructions, large_instructions,
            1.25 * s_large / s_small, f"S={s_small}|S={s_large}",
        )

    async def test_sync_close_connections_is_linear_in_stale_count(self) -> None:
        await self._check("SandboxBatch._close_connections", use_async=False)

    async def test_async_close_connections_is_linear_in_stale_count(self) -> None:
        await self._check("AsyncSandboxBatch._close_connections", use_async=True)


def _container_like(template: object, items: list[_CountingConnection]) -> Any:
    """Returns ``items`` in the same container type the handle initialized, so the test runs
    against whichever container the handle uses (a ``list``, or an insertion-ordered ``dict``
    or ``set``)."""
    if isinstance(template, dict):
        return dict.fromkeys(items)
    if isinstance(template, set):
        return set(items)
    return list(items)


def _assert_requests_within(
    case: unittest.TestCase, label: str, helper: Any, budget: dict[str, int]
) -> None:
    """Fails when ``helper`` saw any request beyond ``budget`` (an upper bound per request
    kind, so removing a request still passes)."""
    sent = collections.Counter(name for name, _a, _k in helper.method_calls)
    over = {name: count for name, count in sent.items() if count > budget.get(name, 0)}
    case.assertEqual(over, {}, f"{label} sent requests beyond {budget}: {dict(sent)}")


class TestSuiteBLifecycleRequests(unittest.IsolatedAsyncioTestCase):
    """Suite B: the batch lifecycle calls send a fixed number of requests, independent of the
    batch's size: attach (get_batch), detach, release, and release_group."""

    RELEASE = {
        "read_batch_lease": 1, "replace_batch_lease": 1, "delete_sandbox_claims_by_label": 1,
        "count_live_sandbox_claims": 1, "delete_batch_lease": 1,
    }
    RELEASE_GROUP = {"delete_sandbox_claims_by_label": 1, "count_live_sandbox_claims": 1}
    DETACH = {"read_batch_lease": 1, "replace_batch_lease": 1}
    ATTACH = {"read_batch_lease": 1, "list_sandbox_claim_objects": 1, "replace_batch_lease": 1}

    def _groups(self, g: int) -> list[BatchGroup]:
        return [BatchGroup(warmpool=f"pool-{i}", size=2) for i in range(g)]

    def test_sync_release_detach_and_release_group_requests_are_fixed(self) -> None:
        for g in (2, 200):
            _client, helper, batch = _new_stopped_sync_batch(self._groups(g), batch_id="b-life")
            helper.reset_mock()
            batch.release_group("pool-0")
            _assert_requests_within(self, f"release_group (G={g})", helper, self.RELEASE_GROUP)
            helper.reset_mock()
            batch.release()
            _assert_requests_within(self, f"release (G={g})", helper, self.RELEASE)
            _client, helper, batch = _new_stopped_sync_batch(self._groups(g), batch_id="b-det")
            helper.reset_mock()
            batch.detach()
            _assert_requests_within(self, f"detach (G={g})", helper, self.DETACH)

    async def test_async_release_detach_and_release_group_requests_are_fixed(self) -> None:
        for g in (2, 200):
            _client, helper, batch = await _new_stopped_async_batch(
                self._groups(g), batch_id="b-life"
            )
            helper.reset_mock()
            await batch.release_group("pool-0")
            _assert_requests_within(self, f"release_group (G={g})", helper, self.RELEASE_GROUP)
            helper.reset_mock()
            await batch.release()
            _assert_requests_within(self, f"release (G={g})", helper, self.RELEASE)
            _client, helper, batch = await _new_stopped_async_batch(
                self._groups(g), batch_id="b-det"
            )
            helper.reset_mock()
            await batch.detach()
            _assert_requests_within(self, f"detach (G={g})", helper, self.DETACH)

    def _attach_helper(self, helper: Any, claims: int) -> None:
        free = k8s_client.V1Lease(
            metadata=k8s_client.V1ObjectMeta(name="batch-b1", resource_version="10", uid="u",
                                             annotations={}),
            spec=k8s_client.V1LeaseSpec(holder_identity="", renew_time=datetime.now(UTC),
                                        lease_duration_seconds=60),
        )
        helper.reset_mock()
        helper.read_batch_lease.side_effect = None
        helper.read_batch_lease.return_value = free
        helper.list_sandbox_claim_objects.return_value = (
            [_ready_claim("b1", i, f"pool-{i % 10}", claims // 10) for i in range(claims)], "5"
        )
        helper.replace_batch_lease.side_effect = None
        helper.replace_batch_lease.return_value = free

    def test_sync_attach_requests_are_fixed(self) -> None:
        for claims in (10, 1000):
            client, helper, _batch = _new_stopped_sync_batch(batch_id="b-any")
            self._attach_helper(helper, claims)
            with patch.object(SandboxBatch, "_start", lambda _self: None):
                client.get_batch("b1")
            _assert_requests_within(self, f"get_batch ({claims} claims)", helper, self.ATTACH)

    async def test_async_attach_requests_are_fixed(self) -> None:
        for claims in (10, 1000):
            client, helper, _batch = await _new_stopped_async_batch(batch_id="b-any")
            self._attach_helper(helper, claims)
            with patch.object(AsyncSandboxBatch, "_start", lambda _self: None):
                await client.get_batch("b1")
            _assert_requests_within(self, f"get_batch ({claims} claims)", helper, self.ATTACH)


class TestSuiteAAttachComplexity(unittest.TestCase):
    """Suite A: attaching to a batch (get_batch) rebuilds its groups and members from the
    listed claims, which must cost time linear in the claim count N and group count G, not
    O(N * G)."""

    SIZES = ((500, 100), (5000, 1000))

    def _attach_state(self, n: int, g: int) -> Callable[[], object]:
        per = n // g
        claims = [_ready_claim("b1", i, f"pool-{i // per}", per) for i in range(n)]

        def run() -> None:
            groups = batch_state_module.reconstruct_groups("b1", claims)
            state = BatchState("b1", groups, n)
            state.seed_from_claims(claims)
            state.count_missing_fill_as_unable()

        return run

    def test_attach_rebuild_is_linear_in_claims_and_groups(self) -> None:
        (n_small, g_small), (n_large, g_large) = self.SIZES
        small = measure(lambda: self._attach_state(n_small, g_small))
        large = measure(lambda: self._attach_state(n_large, g_large))
        size_ratio = n_large / n_small
        assert_scaling(self, "reconstruct_groups + seed_from_claims", small, large,
                       O1_MAX_RATIO * size_ratio,
                       f"N={n_small},G={g_small}|N={n_large},G={g_large}")


class TestSuiteCReleasedTombstonesTarget(unittest.TestCase):
    """Suite C, target for F-35: once a released claim's DELETED event has been seen, a
    long-lived batch should not keep its name. Expected to fail until F-35 is fixed; an
    unexpected pass then fails the run, so the fix removes this marker and the bound in
    TestSuiteCLongLivedChurn tightens with it."""

    @unittest.expectedFailure
    def test_released_tombstones_stay_bounded(self) -> None:
        state = BatchState("btomb", [BatchGroup(warmpool="pool-0", size=0)])
        for ep in range(2000):
            name = state.start_acquire("pool-0")
            obj = _ready_claim("btomb", 0, "pool-0", 0)
            obj["metadata"] = {"name": name, "uid": f"uid-{ep}", "resourceVersion": str(ep + 1)}
            state.upsert_claim(obj)
            state.release_claim(name, "pool-0")
            state.note_deleted(name, state.delete_seq(name))
        state_any: Any = state
        self.assertLessEqual(len(state_any._released), 100)


class _ModuleProxy:
    """Stands in for a stdlib module inside one SDK module, overriding only the given names,
    so a wait can be recorded without patching the module for the whole process."""

    def __init__(self, real: Any, **overrides: Any) -> None:
        self._real = real
        self.__dict__.update(overrides)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


# A healthy wait sleeps until the acquire or Lease deadline (tens of seconds here); a poll caps
# it. Anything shorter than this is a poll, however long the observed idle window is.
MIN_IDLE_WAIT_SECONDS = 5.0
IDLE_WINDOW_SECONDS = 1.0


def _assert_waits_unbounded_by_poll(test: unittest.TestCase, label: str,
                                    waits: list[float | None]) -> None:
    test.assertTrue(waits, f"{label}: never waited on its wakeup (sleeps or spins instead)")
    shortest = min(float("inf") if w is None else w for w in waits)
    test.assertGreaterEqual(
        shortest, MIN_IDLE_WAIT_SECONDS,
        f"{label}: waited at most {shortest:.2f}s at a time while nothing changed (a poll)",
    )


class TestSuiteBRequestLowerBounds(unittest.IsolatedAsyncioTestCase):
    """Suite B: outbound request counts equal their algebraic lower bounds."""

    POOLS_TO_TEMPLATE = {f"pool-{i}": f"tmpl-{i % 2}" for i in range(6)}

    def _claim_batch_budget(self, g: int, t: int) -> dict[str, int]:
        # One read per pool and per distinct template, the Lease create, the leftover-claims
        # LIST, and the client's pool-size lookup (not a request).
        return {
            "api_pool_size": 1, "get_sandbox_warmpool": g, "get_sandbox_template": t,
            "create_batch_lease": 1, "list_sandbox_claim_objects": 1,
        }

    def _warmpool_for(self, name: str, *_a: object, **_kw: object) -> dict[str, Any]:
        return {
            "spec": {"sandboxTemplateRef": {"name": self.POOLS_TO_TEMPLATE[name]}},
            "status": {"readyReplicas": 2},
        }

    def test_sync_claim_batch_reads_each_pool_once_and_each_shared_template_once(self) -> None:
        groups = [BatchGroup(warmpool=p, size=1) for p in self.POOLS_TO_TEMPLATE]
        _client, helper, _batch = _new_stopped_sync_batch(
            [BatchGroup(warmpool="pool-0", size=1)], batch_id="warm"
        )
        helper.reset_mock()
        helper.get_sandbox_warmpool.side_effect = self._warmpool_for
        helper.get_sandbox_template.side_effect = lambda name, *_a, **_k: {
            "metadata": {"name": name}
        }
        with (
            patch.object(SandboxBatch, "_start", lambda _self: None),
            patch.object(SandboxBatch, "_create_worker", lambda _self: None),
        ):
            _client.claim_batch(groups, batch_id="b-perf10", lease_duration=60)
        g = len(groups)
        t = len(set(self.POOLS_TO_TEMPLATE.values()))
        self.assertEqual(helper.get_sandbox_warmpool.call_count, g)
        self.assertEqual(helper.get_sandbox_template.call_count, t)
        _assert_requests_within(self, "claim_batch", helper, self._claim_batch_budget(g, t))

    async def test_async_claim_batch_reads_each_pool_once_and_each_shared_template_once(
        self,
    ) -> None:
        groups = [BatchGroup(warmpool=p, size=1) for p in self.POOLS_TO_TEMPLATE]
        client, helper, _batch = await _new_stopped_async_batch(
            [BatchGroup(warmpool="pool-0", size=1)], batch_id="warm"
        )
        helper.reset_mock()
        helper.get_sandbox_warmpool = AsyncMock(side_effect=self._warmpool_for)
        helper.get_sandbox_template = AsyncMock(
            side_effect=lambda name, *_a, **_k: {"metadata": {"name": name}}
        )

        async def _noop_async(_self: object) -> None:
            return None

        with (
            patch.object(AsyncSandboxBatch, "_start", lambda _self: None),
            patch.object(AsyncSandboxBatch, "_create_worker", _noop_async),
        ):
            await client.claim_batch(groups, batch_id="b-perf10", lease_duration=60)
        g = len(groups)
        t = len(set(self.POOLS_TO_TEMPLATE.values()))
        self.assertEqual(helper.get_sandbox_warmpool.await_count, g)
        self.assertEqual(helper.get_sandbox_template.await_count, t)
        _assert_requests_within(self, "claim_batch", helper, self._claim_batch_budget(g, t))

    def test_sync_conflict_free_renewals_issue_one_put_and_zero_gets_each(self) -> None:
        _client, helper, batch = _new_stopped_sync_batch(batch_id="b-perf8")
        helper.read_batch_lease.reset_mock()
        helper.replace_batch_lease.reset_mock()
        renewals = 5
        for _ in range(renewals):
            self.assertTrue(batch._renew_once())
        self.assertEqual(helper.replace_batch_lease.call_count, renewals)
        self.assertEqual(helper.read_batch_lease.call_count, 0)

    async def test_async_conflict_free_renewals_issue_one_put_and_zero_gets_each(self) -> None:
        _client, helper, batch = await _new_stopped_async_batch(batch_id="b-perf8")
        helper.read_batch_lease = AsyncMock(side_effect=lambda *_a, **_k: batch._lease)
        renewals = 5
        replace = helper.replace_batch_lease
        replace.reset_mock()
        for _ in range(renewals):
            self.assertTrue(await batch._renew_once())
        self.assertEqual(replace.await_count, renewals)
        self.assertEqual(helper.read_batch_lease.await_count, 0)

    EPISODES = 5

    def test_sync_acquire_and_release_send_one_create_and_one_delete_each(self) -> None:
        # An acquire covered by warm credit and a release_member are the per-episode RL path:
        # one claim create and one claim delete each, with no pool, template, or claim reads.
        _client, helper, batch = _new_stopped_sync_batch(batch_id="b-req")
        ready = _ready_on_create(batch)

        def ready_locked(*args: Any, **kwargs: Any) -> None:
            with batch._lock:
                ready(*args, **kwargs)

        helper.create_sandbox_claim.side_effect = ready_locked
        batch._warm_credit.record("pool-a", 100)
        batch.acquire("pool-a", timeout=5)
        helper.reset_mock()
        helper.create_sandbox_claim.side_effect = ready_locked
        members = [batch.acquire("pool-a", timeout=5) for _ in range(self.EPISODES)]
        self.assertEqual(
            [name for name, _a, _k in helper.method_calls],
            ["create_sandbox_claim"] * self.EPISODES,
        )
        helper.reset_mock()
        for member in members:
            batch.release_member(member)
        batch._wait_active_deletes()
        self.assertEqual(
            [name for name, _a, _k in helper.method_calls],
            ["delete_sandbox_claim"] * self.EPISODES,
        )

    def test_sync_blocked_acquire_rechecks_only_when_woken(self) -> None:
        # An acquire waiting on a pending claim should sleep until the watch wakes it, not
        # poll: with W waiting RL workers, each extra recheck takes the batch lock W times.
        _client, helper, batch = _new_stopped_sync_batch(batch_id="b-wait")
        created: list[str] = []

        def pending(claim_name: str, warmpool: str, *_a: object, **_k: object) -> None:
            obj = _ready_claim("wait", 0, warmpool, 0)
            obj["metadata"] = {
                "name": claim_name, "uid": f"uid-{claim_name}", "resourceVersion": "5"
            }
            obj["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
            with batch._lock:
                batch._state.upsert_claim(obj)
            created.append(claim_name)

        helper.create_sandbox_claim.side_effect = pending
        batch._warm_credit.record("pool-a", 10)
        state = batch._state
        checks = 0
        real_check = state.acquired_member

        def counted(claim_name: str) -> Any:
            nonlocal checks
            checks += 1
            return real_check(claim_name)

        state_any: Any = state
        state_any.acquired_member = counted
        result: list[Any] = []
        errors: list[BaseException] = []
        waits: list[float | None] = []

        class RecordingEvent(threading.Event):
            def wait(self, timeout: float | None = None) -> bool:
                waits.append(timeout)
                return super().wait(timeout)

        def acquire() -> None:
            try:
                result.append(batch.acquire("pool-a", timeout=30))
            except BaseException as e:  # surfaced below instead of hanging the test
                errors.append(e)

        # Only the acquire's own waiter is created after this point.
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(
            sandbox_batch_module, "threading", _ModuleProxy(threading, Event=RecordingEvent)
        ))
        waiter = threading.Thread(target=acquire, daemon=True)
        waiter.start()
        give_up = time.monotonic() + 10
        while not created and not errors and time.monotonic() < give_up:
            time.sleep(0.01)
        self.assertEqual(errors, [])
        self.assertTrue(created, "acquire never sent its create")
        time.sleep(0.05)
        idle_start = checks
        time.sleep(IDLE_WINDOW_SECONDS)
        idle_checks = checks - idle_start
        obj = _ready_claim("wait", 0, "pool-a", 0)
        obj["metadata"] = {"name": created[0], "uid": f"uid-{created[0]}", "resourceVersion": "6"}
        with batch._changed:
            state.upsert_claim(obj)
            batch._wake_acquire(created[0])
            batch._changed.notify_all()
        waiter.join(10)
        stack.close()
        self.assertEqual(len(result), 1)
        # Nothing changed while it waited, so it must not have rechecked; allow one spurious
        # wakeup. The recorded wait timeouts catch slower polls that this window can't.
        self.assertLessEqual(idle_checks, 1, f"{idle_checks} rechecks in 1 s with no change")
        _assert_waits_unbounded_by_poll(self, "SandboxBatch.acquire (pending claim)", waits)

    async def test_async_blocked_acquire_rechecks_only_when_woken(self) -> None:
        _client, helper, batch = await _new_stopped_async_batch(batch_id="b-wait")
        created: list[str] = []

        async def pending(claim_name: str, warmpool: str, *_a: object, **_k: object) -> None:
            obj = _ready_claim("wait", 0, warmpool, 0)
            obj["metadata"] = {
                "name": claim_name, "uid": f"uid-{claim_name}", "resourceVersion": "5"
            }
            obj["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
            batch._state.upsert_claim(obj)
            created.append(claim_name)

        helper.create_sandbox_claim = pending
        batch._warm_credit.record("pool-a", 10)
        state = batch._state
        checks = 0
        real_check = state.acquired_member

        def counted(claim_name: str) -> Any:
            nonlocal checks
            checks += 1
            return real_check(claim_name)

        state_any: Any = state
        state_any.acquired_member = counted
        waits: list[float | None] = []
        real_wait_for = asyncio.wait_for

        def recording_wait_for(aw: Any, timeout: float | None) -> Any:
            # Only the acquire's own wakeup wait, not pool checks or pacing.
            if getattr(aw, "__qualname__", "") == "Event.wait":
                waits.append(timeout)
            return real_wait_for(aw, timeout)

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(
            async_sandbox_batch_module, "asyncio",
            _ModuleProxy(asyncio, wait_for=recording_wait_for),
        ))
        task = asyncio.ensure_future(batch.acquire("pool-a", timeout=30))
        give_up = time.monotonic() + 10
        while not created and not task.done() and time.monotonic() < give_up:
            await asyncio.sleep(0.01)
        if task.done():
            task.result()
        self.assertTrue(created, "acquire never sent its create")
        await asyncio.sleep(0.05)
        idle_start = checks
        await asyncio.sleep(IDLE_WINDOW_SECONDS)
        idle_checks = checks - idle_start
        obj = _ready_claim("wait", 0, "pool-a", 0)
        obj["metadata"] = {"name": created[0], "uid": f"uid-{created[0]}", "resourceVersion": "6"}
        async with batch._lock:
            state.upsert_claim(obj)
            batch._wake_acquire(created[0])
        await asyncio.wait_for(task, 10)
        stack.close()
        self.assertLessEqual(idle_checks, 1, f"{idle_checks} rechecks in 1 s with no change")
        _assert_waits_unbounded_by_poll(self, "AsyncSandboxBatch.acquire (pending claim)", waits)

    def test_sync_idle_consumer_rechecks_only_when_woken(self) -> None:
        # events(), group_outcomes(), and wait_for_quorum() wait in _next for the next change;
        # with nothing changing they must sleep, not poll the batch lock.
        _client, _helper, batch = _new_stopped_sync_batch(
            [BatchGroup(warmpool="pool-a", size=1)], batch_id="b-idle"
        )
        state = batch._state
        state.set_mode(ConsumerMode.STREAM)
        pops = 0

        def counted_pop() -> Any:
            nonlocal pops
            pops += 1
            return state.pop_event()

        waits: list[float | None] = []
        real_wait = batch._changed.wait

        def recording_wait(timeout: float | None = None) -> bool:
            waits.append(timeout)
            return real_wait(timeout)

        changed_any: Any = batch._changed
        changed_any.wait = recording_wait
        result: list[Any] = []
        consumer = threading.Thread(
            target=lambda: result.append(batch._next(counted_pop, state.events_done)),
            daemon=True,
        )
        consumer.start()
        time.sleep(0.05)
        idle_start = pops
        time.sleep(IDLE_WINDOW_SECONDS)
        idle_pops = pops - idle_start
        with batch._changed:
            state.upsert_claim(_ready_claim(batch.batch_id, 0, "pool-a", 1))
            batch._changed.notify_all()
        consumer.join(10)
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0])
        self.assertLessEqual(idle_pops, 1, f"{idle_pops} rechecks in 1 s with no change")
        _assert_waits_unbounded_by_poll(self, "SandboxBatch._next (idle)", waits)

    async def test_async_idle_consumer_rechecks_only_when_woken(self) -> None:
        _client, _helper, batch = await _new_stopped_async_batch(
            [BatchGroup(warmpool="pool-a", size=1)], batch_id="b-idle"
        )
        state = batch._state
        state.set_mode(ConsumerMode.STREAM)
        pops = 0

        def counted_pop() -> Any:
            nonlocal pops
            pops += 1
            return state.pop_event()

        waits: list[float | None] = []
        real_wait_for = asyncio.wait_for

        def recording_wait_for(aw: Any, timeout: float | None) -> Any:
            if getattr(aw, "__qualname__", "") == "Condition.wait":
                waits.append(timeout)
            return real_wait_for(aw, timeout)

        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(
            async_sandbox_batch_module, "asyncio",
            _ModuleProxy(asyncio, wait_for=recording_wait_for),
        ))
        task = asyncio.ensure_future(batch._next(counted_pop, state.events_done))
        await asyncio.sleep(0.05)
        idle_start = pops
        await asyncio.sleep(IDLE_WINDOW_SECONDS)
        idle_pops = pops - idle_start
        async with batch._changed:
            state.upsert_claim(_ready_claim(batch.batch_id, 0, "pool-a", 1))
            batch._changed.notify_all()
        self.assertIsNotNone(await asyncio.wait_for(task, 10))
        stack.close()
        self.assertLessEqual(idle_pops, 1, f"{idle_pops} rechecks in 1 s with no change")
        _assert_waits_unbounded_by_poll(self, "AsyncSandboxBatch._next (idle)", waits)

    def test_sync_watch_reconnects_never_relist(self) -> None:
        # A watch that ends at its timeout reconnects from its resourceVersion; only a 410
        # needs a LIST. K normal reconnects must send K watches and no LIST.
        _client, helper, batch = _new_stopped_sync_batch(batch_id="b-recon")
        helper.reset_mock()
        streams = 0
        cycles = 5

        def watch(*_a: object, **_kw: object) -> Iterator[dict[str, Any]]:
            nonlocal streams
            streams += 1
            yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": str(streams)}}}
            if streams >= cycles:
                batch._watch_stop.set()

        helper.watch_sandbox_claims.side_effect = watch
        with patch.object(sandbox_batch_module, "_SHORT_WATCH_SECONDS", 0.0):
            batch._watch_loop()
        self.assertEqual(streams, cycles)
        self.assertEqual(helper.list_sandbox_claim_objects.call_count, 0)
        # Each watch asks the server to hold the stream at least 30 s, so a healthy batch
        # sends at most two watch requests a minute.
        for call in helper.watch_sandbox_claims.call_args_list:
            server_timeout = call.args[3]
            self.assertGreaterEqual(server_timeout, 30)
            self.assertGreater(call.kwargs["_request_timeout"], server_timeout)

    async def test_async_watch_reconnects_never_relist(self) -> None:
        _client, helper, batch = await _new_stopped_async_batch(batch_id="b-recon")
        helper.list_sandbox_claim_objects.reset_mock()
        streams = 0
        cycles = 5

        timeouts: list[tuple[Any, Any]] = []

        async def watch(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            nonlocal streams
            streams += 1
            timeouts.append((args[3], kwargs["_request_timeout"]))
            yield {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": str(streams)}}}
            if streams >= cycles:
                raise asyncio.CancelledError()

        helper.watch_sandbox_claims = watch
        with patch.object(async_sandbox_batch_module, "_SHORT_WATCH_SECONDS", 0.0):
            with contextlib.suppress(asyncio.CancelledError):
                await batch._watch_loop()
        self.assertEqual(streams, cycles)
        self.assertEqual(helper.list_sandbox_claim_objects.await_count, 0)
        for server_timeout, request_timeout in timeouts:
            self.assertGreaterEqual(server_timeout, 30)
            self.assertGreater(request_timeout, server_timeout)

    def test_sync_uncovered_acquire_with_open_slot_sends_only_its_create(self) -> None:
        # PERF-13: with no warm credit but an open pacing slot, acquire creates at once without
        # re-reading the warm pool.
        _client, helper, batch = _new_stopped_sync_batch(batch_id="b-slot")
        ready = _ready_on_create(batch)

        def ready_locked(*args: Any, **kwargs: Any) -> None:
            with batch._lock:
                ready(*args, **kwargs)

        helper.create_sandbox_claim.side_effect = ready_locked
        batch._warm_credit.record("pool-a", 1)
        batch.acquire("pool-a", timeout=5)
        helper.reset_mock()
        helper.create_sandbox_claim.side_effect = ready_locked
        for _ in range(self.EPISODES):
            batch._next_create_at = 0.0
            batch.acquire("pool-a", timeout=5)
        self.assertEqual(
            [name for name, _a, _k in helper.method_calls],
            ["create_sandbox_claim"] * self.EPISODES,
        )

    async def test_async_uncovered_acquire_with_open_slot_sends_only_its_create(self) -> None:
        _client, helper, batch = await _new_stopped_async_batch(batch_id="b-slot")
        helper.create_sandbox_claim = AsyncMock(side_effect=_ready_on_create(batch))
        batch._warm_credit.record("pool-a", 1)
        await batch.acquire("pool-a", timeout=5)
        helper.reset_mock()
        for _ in range(self.EPISODES):
            batch._next_create_at = 0.0
            await batch.acquire("pool-a", timeout=5)
        self.assertEqual(
            [str(c).split("(")[0] for c in helper.mock_calls],
            ["call.create_sandbox_claim"] * self.EPISODES,
        )

    async def test_async_acquire_and_release_send_one_create_and_one_delete_each(self) -> None:
        _client, helper, batch = await _new_stopped_async_batch(batch_id="b-req")
        ready = _ready_on_create(batch)
        helper.create_sandbox_claim = AsyncMock(side_effect=ready)
        batch._warm_credit.record("pool-a", 100)
        await batch.acquire("pool-a", timeout=5)
        helper.reset_mock()
        members = [await batch.acquire("pool-a", timeout=5) for _ in range(self.EPISODES)]
        self.assertEqual(
            [str(c).split("(")[0] for c in helper.mock_calls],
            ["call.create_sandbox_claim"] * self.EPISODES,
        )
        helper.reset_mock()
        for member in members:
            await batch.release_member(member)
        await batch._wait_active_deletes()
        self.assertEqual(
            [str(c).split("(")[0] for c in helper.mock_calls],
            ["call.delete_sandbox_claim"] * self.EPISODES,
        )

    def test_batch_create_and_delete_call_sites_log_at_debug(self) -> None:
        # PERF-9: an INFO record per claim turns a 2,000-claim batch into 4,000 driver log lines.
        for module in (sandbox_batch_module, async_sandbox_batch_module):
            tree = ast.parse(inspect.getsource(module))
            sites = 0
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"create_sandbox_claim", "delete_sandbox_claim"}
                ):
                    continue
                sites += 1
                level = next((kw.value for kw in node.keywords if kw.arg == "log_level"), None)
                with self.subTest(module=module.__name__, line=node.lineno):
                    self.assertIsNotNone(level, "batch call site relies on the INFO default")
                    assert level is not None
                    self.assertTrue(ast.unparse(level).endswith("DEBUG"), ast.unparse(level))
            self.assertGreaterEqual(sites, 2, module.__name__)

    def test_helper_debug_creates_emit_zero_info_records(self) -> None:
        with (
            patch("k8s_agent_sandbox.k8s_helper.config"),
            patch("k8s_agent_sandbox.k8s_helper.client.CustomObjectsApi"),
            patch("k8s_agent_sandbox.k8s_helper.client.CoreV1Api"),
        ):
            helper = K8sHelper()
            helper_any: Any = helper
            helper_any._batch_custom_objects_api = MagicMock()
            logger = logging.getLogger("k8s_agent_sandbox")
            records: list[logging.LogRecord] = []

            class _Collect(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    records.append(record)

            handler = _Collect(level=logging.INFO)
            previous = logger.level
            logger.addHandler(handler)
            logger.setLevel(logging.DEBUG)
            try:
                for i in range(100):
                    helper.create_sandbox_claim(
                        f"b-{i}", "pool-a", "default", log_level=logging.DEBUG, batch=True
                    )
            finally:
                logger.removeHandler(handler)
                logger.setLevel(previous)
        self.assertEqual([r.getMessage() for r in records if r.levelno >= logging.INFO], [])


class MemoryWindow:
    """Measures allocations inside a block with ``tracemalloc``, relative to the block's start.

    Reuses tracing that is already on (``PYTHONTRACEMALLOC=1`` or ``-X tracemalloc``) by
    resetting its peak and subtracting the block's starting size, and stops tracing on exit only
    if this window started it. It never calls ``clear_traces()``, which hangs under pytest with
    tracing enabled from startup on CPython 3.13.
    """

    def __enter__(self) -> MemoryWindow:
        gc.collect()
        self._started = not tracemalloc.is_tracing()
        if self._started:
            tracemalloc.start()
        tracemalloc.reset_peak()
        self._base, _ = tracemalloc.get_traced_memory()
        return self

    def current(self) -> int:
        return tracemalloc.get_traced_memory()[0] - self._base

    def peak(self) -> int:
        return tracemalloc.get_traced_memory()[1] - self._base

    def __exit__(self, *_exc: object) -> None:
        if self._started:
            tracemalloc.stop()


class _Page(dict[str, Any]):
    """A ``dict`` subclass, since plain dicts can't be weakly referenced."""


def _claim_page(start: int, size: int, cont: str | None) -> _Page:
    items = [
        {
            "metadata": {
                "name": f"b1-{i}",
                "managedFields": [{"manager": "sdk", "fieldsV1": {"f:spec": {}}}],
                "annotations": {"note": "x" * 64},
            },
            "spec": {"warmPoolRef": {"name": "pool-a"}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
        for i in range(start, start + size)
    ]
    return _Page({"items": items, "metadata": {"continue": cont} if cont else {}})


@patch("k8s_agent_sandbox.k8s_helper.client.CoreV1Api")
@patch("k8s_agent_sandbox.k8s_helper.client.CustomObjectsApi")
@patch("k8s_agent_sandbox.k8s_helper.config")
class TestSuiteCMemoryBounds(unittest.TestCase):
    """Suite C: memory held by list, count, and deletecollection helpers is bounded by one page
    or one chunk, independent of how many claims the batch has."""

    PAGE = 500

    def _count_peak(self, pages: int, mock_api_cls: MagicMock) -> int:
        page_size = self.PAGE
        produced = 0
        prev_ref: weakref.ref[_Page] | None = None
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api

        def next_page(**_kw: object) -> _Page:
            nonlocal produced, prev_ref
            if prev_ref is not None:
                self.assertIsNone(prev_ref(), "previous page still referenced")
            idx = produced
            produced += 1
            cont = f"t{idx}" if idx + 1 < pages else None
            page = _claim_page(idx * page_size, page_size, cont)
            prev_ref = weakref.ref(page)
            return page

        mock_api.list_namespaced_custom_object.side_effect = next_page
        helper = K8sHelper()
        with MemoryWindow() as window:
            live = helper.count_live_sandbox_claims("default", "agents.x-k8s.io/batch-id=b1")
            peak = window.peak()
        self.assertEqual(live, pages * page_size)
        return peak

    def test_count_live_sandbox_claims_holds_one_page_at_a_time(
        self, _config: MagicMock, mock_api_cls: MagicMock, _core: MagicMock
    ) -> None:
        # PERF-5: counting 5,000 claims must not hold 10 pages at once.
        one = self._count_peak(1, mock_api_cls)
        ten = self._count_peak(10, mock_api_cls)
        self.assertLessEqual(ten, 1.35 * one, f"peak {one:,} B for 1 page vs {ten:,} B for 10")

    def test_list_sandbox_claim_objects_strips_managed_fields_on_every_page(
        self, _config: MagicMock, mock_api_cls: MagicMock, _core: MagicMock
    ) -> None:
        # PERF-6: managedFields roughly doubles each claim dict and no caller reads it.
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        pages = [_claim_page(0, 3, "t0"), _claim_page(3, 3, "t1"), _claim_page(6, 3, None)]
        mock_api.list_namespaced_custom_object.side_effect = lambda **_kw: pages.pop(0)
        items, _rv = K8sHelper().list_sandbox_claim_objects(
            "default", "agents.x-k8s.io/batch-id=b1"
        )
        self.assertEqual(len(items), 9)
        self.assertEqual([i for i in items if "managedFields" in i["metadata"]], [])

    def test_delete_sandbox_claims_by_label_drains_large_body_in_bounded_memory(
        self, _config: MagicMock, mock_api_cls: MagicMock, _core: MagicMock
    ) -> None:
        # PERF-7: the deletecollection response lists every deleted claim; parsing it would hold
        # the whole body plus its decoded objects.
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        response = mock_api.delete_collection_namespaced_custom_object.return_value
        response.closed = True
        chunk_size = 64 * 1024
        chunks = 128  # 8 MiB in total
        consumed = 0

        def stream(**_kw: object) -> Iterator[bytes]:
            nonlocal consumed
            for _ in range(chunks):
                consumed += 1
                yield b"x" * chunk_size

        response.stream.side_effect = stream
        helper = K8sHelper()
        with MemoryWindow() as window:
            helper.delete_sandbox_claims_by_label("default", "agents.x-k8s.io/batch-id=b1")
            peak = window.peak()
        call = mock_api.delete_collection_namespaced_custom_object.call_args
        # Streamed without preloading, drained to the end, then the connection is released.
        self.assertIs(call.kwargs["_preload_content"], False)
        self.assertEqual(consumed, chunks)
        self.assertLess(peak, 512 * 1024, f"peak {peak:,} B draining {chunks * chunk_size:,} B")
        response.release_conn.assert_called_once()


class TestSuiteCLongLivedChurn(unittest.TestCase):
    """Suite C: a long-lived dynamic batch keeps no per-episode state beyond one released-name
    tombstone per claim.

    The tombstones (``BatchState._released``) themselves grow without bound (F-35, about 156 B
    per episode, roughly 1.5 GB per 10M episodes). This test pins that count, so it can only
    shrink, and measures everything else with the tombstones removed, so any other per-episode
    leak, down to one retained reference (8 B), fails at a 4 B/episode bound.
    """

    def test_acquire_release_churn_retains_only_released_tombstones(self) -> None:
        warmup, episodes = 50, 5000
        state = BatchState("blong", [BatchGroup(warmpool="pool-0", size=0)])

        def episode(ep: int) -> None:
            name = state.start_acquire("pool-0")
            obj = _ready_claim("blong", 0, "pool-0", 0)
            obj["metadata"] = {"name": name, "uid": f"uid-{ep}", "resourceVersion": str(ep + 1)}
            state.upsert_claim(obj)
            self.assertIsNotNone(state.acquired_member(name))
            state.release_claim(name, "pool-0")
            state.note_deleted(name, state.delete_seq(name))

        for ep in range(warmup):
            episode(ep)
        state_any: Any = state
        with MemoryWindow() as window:
            for ep in range(warmup, warmup + episodes):
                episode(ep)
            self.assertEqual(len(state_any._members), 0)
            self.assertEqual(len(state_any._pending_deletes), 0)
            self.assertEqual(len(state_any._acquiring), 0)
            tombstones = len(state_any._released)
            with_tombstones = window.current()
            state_any._released.clear()
            gc.collect()
            without_tombstones = window.current()
        self.assertLessEqual(tombstones, warmup + episodes)
        # One short name per tombstone measures about 156 B/episode; a heavier tombstone (e.g.
        # a whole Member per released claim) multiplies that.
        self.assertLess(
            with_tombstones / episodes, 320, f"{with_tombstones / episodes:.1f} B/episode"
        )
        per_episode = without_tombstones / episodes
        self.assertLess(
            per_episode,
            4,
            f"{per_episode:.1f} B/episode retained besides tombstones "
            f"({with_tombstones / episodes:.1f} B/episode with them)",
        )


if __name__ == "__main__":
    unittest.main()
