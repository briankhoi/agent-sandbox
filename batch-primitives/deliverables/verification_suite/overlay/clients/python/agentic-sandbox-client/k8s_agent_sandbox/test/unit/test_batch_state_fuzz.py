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

"""
Stateful Hypothesis property-based fuzzer for BatchState (Pipeline 4).

Exercises arbitrary interleavings of watch events (ADDED, MODIFIED, DELETED,
readiness flaps, pod IP swaps, 410 relists), consumer modes (STREAM, GROUP,
QUORUM), fill timeouts, dynamic acquire/drop cycles, member/group/not-ready
releases, monotonic delete sequences, and re-attached batch recovery against
BatchInvariantOracle and ReferenceBatchModel.
"""

from typing import Any
import unittest
import zlib

from hypothesis import given, seed, settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    get_state_machine_test,
    initialize,
    invariant,
    precondition,
    rule,
)

from k8s_agent_sandbox.batch_state import BatchState, ConsumerMode
from k8s_agent_sandbox.constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
)
from k8s_agent_sandbox.exceptions import BatchError, TerminalMemberError
from k8s_agent_sandbox.models import BatchGroup
from k8s_agent_sandbox.test.unit.batch_invariant_oracle import (
    BatchInvariantOracle,
    ReferenceBatchModel,
)


def _make_claim_obj(
    *,
    claim_name: str,
    warmpool: str,
    group_spec: BatchGroup | None,
    status_kind: str,
    pod_gen: int = 1,
    terminating: bool = False,
) -> dict[str, Any]:
    """Builds a synthetic Kubernetes SandboxClaim dict for BatchState fuzzing."""
    annotations: dict[str, str] = {}
    if group_spec is not None:
        annotations[BATCH_GROUP_SIZE_ANNOTATION] = str(group_spec.size)
        annotations[BATCH_GROUP_MIN_READY_ANNOTATION] = str(group_spec.min_ready or 0)

    metadata: dict[str, Any] = {"name": claim_name, "annotations": annotations}
    if terminating:
        metadata["deletionTimestamp"] = "2026-11-07T00:00:00Z"

    status: dict[str, Any] = {}
    if status_kind == "unreconciled":
        status = {}
    elif status_kind == "warm_conflict":
        status = {
            "conditions": [{"type": "Ready", "status": "False", "reason": "AdoptionConflict"}],
        }
    elif status_kind == "cold_pending":
        status = {
            "conditions": [{"type": "Ready", "status": "False", "reason": "SandboxNotReady"}],
            "sandbox": {"name": f"sbx-{claim_name}-{pod_gen}"},
        }
    elif status_kind == "ready":
        host_octet = (zlib.crc32(claim_name.encode("utf-8")) % 240) + 1
        status = {
            "conditions": [{"type": "Ready", "status": "True", "reason": "SandboxReady"}],
            "sandbox": {
                "name": f"sbx-{claim_name}-{pod_gen}",
                "podIPs": [f"10.0.{pod_gen % 250}.{host_octet}"],
                "serviceFQDN": f"{claim_name}.default.svc.cluster.local",
            },
        }
    elif status_kind == "flapped_unready":
        host_octet = (zlib.crc32(claim_name.encode("utf-8")) % 240) + 1
        status = {
            "conditions": [{"type": "Ready", "status": "False", "reason": "DependenciesNotReady"}],
            "sandbox": {
                "name": f"sbx-{claim_name}-{pod_gen}",
                "podIPs": [f"10.0.{pod_gen % 250}.{host_octet}"],
            },
        }
    elif status_kind == "terminal_failed":
        status = {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "reason": "PodFailed",
                    "message": "container exited 1",
                }
            ],
            "sandbox": {"name": f"sbx-{claim_name}-{pod_gen}"},
        }
    else:
        raise ValueError(f"unknown status_kind: {status_kind!r}")

    return {
        "metadata": metadata,
        "spec": {"warmPoolRef": {"name": warmpool}},
        "status": status,
    }


_GROUP_CONFIG_STRATEGY = st.lists(
    st.tuples(
        st.sampled_from(["gpu-a", "gpu-b", "cpu-a"]),
        st.sampled_from([(2, 2), (3, 2), (3, 3), (1, 1), (2, 1), (0, 0)]),
    ),
    min_size=1,
    max_size=3,
    unique_by=lambda t: t[0],
).map(
    lambda items: [
        BatchGroup(warmpool=pool, size=size_min[0], min_ready=size_min[1])
        for pool, size_min in items
    ]
)

_STATUS_KINDS = st.sampled_from([
    "unreconciled",
    "warm_conflict",
    "cold_pending",
    "ready",
    "flapped_unready",
    "terminal_failed",
])


class _BaseBatchStateMachine(RuleBasedStateMachine):
    """Shared state-machine harness driving BatchState transitions."""

    ALLOW_GROUP_TIMEOUT: bool = True

    def __init__(self) -> None:
        super().__init__()
        self.batch_id = "fuzzbatch"
        self.state: BatchState | None = None
        self.ref: ReferenceBatchModel | None = None
        self.planned_fill: list[tuple[str, BatchGroup]] = []
        self.known_claims: dict[str, tuple[str, BatchGroup | None]] = {}
        self.in_flight_acquires: list[tuple[str, str]] = []
        self.checking_pools: list[str] = []
        self.seen_delete_seqs: list[tuple[str, int]] = []
        self.skipped_fill_indices: set[int] = set()
        self.destructive_mode: bool = True

    @initialize(
        groups=_GROUP_CONFIG_STRATEGY,
        extra_next_ordinal=st.integers(min_value=0, max_value=5),
        initial_mode=st.sampled_from([
            None,
            ConsumerMode.STREAM,
            ConsumerMode.GROUP,
            ConsumerMode.QUORUM,
        ]),
        destructive_mode=st.booleans(),
    )
    def init_batch(
        self,
        groups: list[BatchGroup],
        extra_next_ordinal: int,
        initial_mode: ConsumerMode | None,
        destructive_mode: bool,
    ) -> None:
        self.destructive_mode = destructive_mode
        total_size = sum(g.size for g in groups)
        init_ord = total_size + extra_next_ordinal
        self.state = BatchState(self.batch_id, groups, next_ordinal=init_ord)
        self.ref = ReferenceBatchModel(
            batch_id=self.batch_id,
            initial_next_ordinal=init_ord,
        )
        self.planned_fill = []
        ordinal = 0
        for g in groups:
            for _ in range(g.size):
                cname = f"{self.batch_id}-{ordinal}"
                ordinal += 1
                self.planned_fill.append((cname, g))
                self.known_claims[cname] = (g.warmpool, g)
        if initial_mode is not None:
            self.state.set_mode(initial_mode)

    @precondition(lambda self: self.state is not None and len(self.planned_fill) > 0)
    @rule(
        idx=st.integers(min_value=0, max_value=20),
        status_kind=st.sampled_from([
            "ready",
            "ready",
            "cold_pending",
            "unreconciled",
            "warm_conflict",
            "flapped_unready",
            "terminal_failed",
        ]),
        pod_gen=st.integers(min_value=1, max_value=3),
        terminating=st.sampled_from([False, False, False, False, True]),
    )
    def upsert_fill_claim(
        self, idx: int, status_kind: str, pod_gen: int, terminating: bool
    ) -> None:
        assert self.state is not None
        pos = idx % len(self.planned_fill)
        if pos in self.skipped_fill_indices:
            return
        cname, group = self.planned_fill[pos]
        obj = _make_claim_obj(
            claim_name=cname,
            warmpool=group.warmpool,
            group_spec=group,
            status_kind=status_kind,
            pod_gen=pod_gen,
            terminating=terminating,
        )
        self.state.upsert_claim(obj)

    @precondition(
        lambda self: self.state is not None and self.destructive_mode and len(self.planned_fill) > 0
    )
    @rule(idx=st.integers(min_value=0, max_value=20))
    def mark_fill_or_dynamic_lost(self, idx: int) -> None:
        assert self.state is not None
        names = list(self.known_claims.keys())
        if not names:
            return
        cname = names[idx % len(names)]
        self.state.mark_lost(cname)

    @precondition(
        lambda self: self.state is not None and self.destructive_mode and len(self.planned_fill) > 0
    )
    @rule(idx=st.integers(min_value=0, max_value=20))
    def record_fill_create_failure(self, idx: int) -> None:
        assert self.state is not None
        pos = idx % len(self.planned_fill)
        if pos in self.skipped_fill_indices:
            return
        cname, group = self.planned_fill[pos]
        self.state.record_create_failure(cname, group.warmpool, "403 Forbidden")

    @precondition(lambda self: self.state is not None and len(self.planned_fill) > 0)
    @rule(idx=st.integers(min_value=0, max_value=20))
    def skip_unstarted_fill_create(self, idx: int) -> None:
        assert self.state is not None
        pos = idx % len(self.planned_fill)
        if pos in self.skipped_fill_indices:
            return
        cname, group = self.planned_fill[pos]
        if (
            cname not in self.state._members
            and cname not in self.state._released
            and self.state.group_failed(group.warmpool)
        ):
            self.skipped_fill_indices.add(pos)
            self.state.record_skipped_create()

    @precondition(lambda self: self.state is not None and self.state._mode is None)
    @rule(mode=st.sampled_from([ConsumerMode.STREAM, ConsumerMode.GROUP, ConsumerMode.QUORUM]))
    def set_consumer_mode(self, mode: ConsumerMode) -> None:
        assert self.state is not None
        if not self.ALLOW_GROUP_TIMEOUT and mode is ConsumerMode.GROUP and self.state._fill_expired:
            return
        self.state.set_mode(mode)

    @precondition(lambda self: self.state is not None)
    @rule()
    def expire_fill_deadline(self) -> None:
        assert self.state is not None
        if not self.ALLOW_GROUP_TIMEOUT and self.state._mode is ConsumerMode.GROUP:
            return
        self.state.expire_fill()

    @precondition(lambda self: self.state is not None)
    @rule(pool=st.sampled_from(["gpu-a", "gpu-b", "cpu-a", "dyn-pool"]))
    def dynamic_acquire_lifecycle(self, pool: str) -> None:
        assert self.state is not None
        self.state.enter_acquire(pool)
        self.checking_pools.append(pool)
        self.state.exit_acquire(self.checking_pools.pop())
        cname = self.state.start_acquire(pool)
        self.known_claims[cname] = (pool, None)
        self.in_flight_acquires.append((cname, pool))

    @precondition(lambda self: self.state is not None and len(self.in_flight_acquires) > 0)
    @rule(
        idx=st.integers(min_value=0, max_value=20),
        status_kind=_STATUS_KINDS,
        pod_gen=st.integers(min_value=1, max_value=3),
    )
    def advance_dynamic_acquire(self, idx: int, status_kind: str, pod_gen: int) -> None:
        assert self.state is not None
        cname, pool = self.in_flight_acquires[idx % len(self.in_flight_acquires)]
        obj = _make_claim_obj(
            claim_name=cname,
            warmpool=pool,
            group_spec=None,
            status_kind=status_kind,
            pod_gen=pod_gen,
        )
        self.state.upsert_claim(obj)
        try:
            got = self.state.acquired_member(cname)
            if got is not None:
                self.in_flight_acquires = [
                    pair for pair in self.in_flight_acquires if pair[0] != cname
                ]
        except (BatchError, TerminalMemberError):
            self.state.drop_acquired(cname, created=True, retried=False)
            self.in_flight_acquires = [pair for pair in self.in_flight_acquires if pair[0] != cname]

    @precondition(lambda self: self.state is not None and len(self.in_flight_acquires) > 0)
    @rule(
        idx=st.integers(min_value=0, max_value=20),
        created=st.booleans(),
        retried=st.booleans(),
    )
    def drop_dynamic_acquire(self, idx: int, created: bool, retried: bool) -> None:
        assert self.state is not None
        cname, _ = self.in_flight_acquires.pop(idx % len(self.in_flight_acquires))
        self.state.drop_acquired(cname, created=created, retried=retried)
        seq = self.state.delete_seq(cname)
        if seq:
            self.seen_delete_seqs.append((cname, seq))

    @precondition(
        lambda self: self.state is not None and self.destructive_mode and len(self.known_claims) > 0
    )
    @rule(idx=st.integers(min_value=0, max_value=30))
    def release_single_member(self, idx: int) -> None:
        assert self.state is not None
        names = list(self.known_claims.keys())
        cname = names[idx % len(names)]
        try:
            self.state.release_member(cname)
            seq = self.state.delete_seq(cname)
            if seq:
                self.seen_delete_seqs.append((cname, seq))
        except ValueError:
            pass

    @precondition(lambda self: self.state is not None and self.destructive_mode)
    @rule()
    def release_not_ready_members(self) -> None:
        assert self.state is not None
        to_del = self.state.release_not_ready()
        for cname in to_del:
            seq = self.state.delete_seq(cname)
            if seq:
                self.seen_delete_seqs.append((cname, seq))

    @precondition(lambda self: self.state is not None and self.destructive_mode)
    @rule(pool=st.sampled_from(["gpu-a", "gpu-b", "cpu-a", "dyn-pool"]))
    def release_entire_group(self, pool: str) -> None:
        assert self.state is not None
        if self.state.has_pool(pool):
            self.state.release_group(pool)

    @precondition(lambda self: self.state is not None and len(self.seen_delete_seqs) > 0)
    @rule(idx=st.integers(min_value=0, max_value=20), use_stale=st.booleans())
    def complete_claim_delete(self, idx: int, use_stale: bool) -> None:
        assert self.state is not None
        cname, seq = self.seen_delete_seqs[idx % len(self.seen_delete_seqs)]
        self.state.note_deleted(cname, max(0, seq - 1) if use_stale else seq)

    @precondition(
        lambda self: self.state is not None and self.destructive_mode and len(self.known_claims) > 0
    )
    @rule(keep_mask=st.integers(min_value=0, max_value=63))
    def resync_on_watch_410(self, keep_mask: int) -> None:
        assert self.state is not None
        objs: list[dict[str, Any]] = []
        for i, (cname, (pool, group)) in enumerate(self.known_claims.items()):
            if int(cname.split("-")[-1]) in self.skipped_fill_indices:
                continue
            if (keep_mask >> (i % 6)) & 1:
                existing = self.state.get_member(cname)
                sk = "ready" if (existing and existing.state.value == "ready") else "cold_pending"
                objs.append(
                    _make_claim_obj(
                        claim_name=cname,
                        warmpool=pool,
                        group_spec=group,
                        status_kind=sk,
                    )
                )
        self.state.resync_from_list(objs)
        self.state.take_exhausted_pools()

    @invariant()
    def verify_all_batch_state_invariants(self) -> None:
        if self.state is None or self.ref is None:
            return
        BatchInvariantOracle.assert_all(
            self.state,
            self.ref,
            in_flight_acquire_names={cname for cname, _ in self.in_flight_acquires},
        )


def _group_timeout_resolves_waiting_ready() -> bool:
    """Probes whether F-12 (_time_out_undecided_groups resolving _waiting) is fixed."""
    grp = BatchGroup(warmpool="probe-p", size=2, min_ready=2)
    probe = BatchState("probeb", [grp], next_ordinal=2)
    probe.set_mode(ConsumerMode.GROUP)
    probe.upsert_claim(
        _make_claim_obj(
            claim_name="probeb-0",
            warmpool="probe-p",
            group_spec=grp,
            status_kind="ready",
        )
    )
    probe.expire_fill()
    return "probeb-0" in probe._resolved


_GROUP_TIMEOUT_FIXED = _group_timeout_resolves_waiting_ready()


@settings(
    max_examples=200,
    stateful_step_count=50,
    deadline=None,
    derandomize=True,
    database=None,
)
class FullBatchStateFuzzer(_BaseBatchStateMachine):
    """Unfiltered fuzzer across all BatchState rules; catches F-12 (GROUP timeout leak)."""

    ALLOW_GROUP_TIMEOUT = True


@settings(max_examples=200, stateful_step_count=50, deadline=None, derandomize=True)
class HealthyCoreBatchStateFuzzer(_BaseBatchStateMachine):
    """Exercises non-GROUP-timeout transitions (and GROUP timeout automatically post-F-12 fix)."""

    ALLOW_GROUP_TIMEOUT = _GROUP_TIMEOUT_FIXED


@settings(max_examples=150, stateful_step_count=40, deadline=None, derandomize=True)
class ReattachedBatchStateFuzzer(RuleBasedStateMachine):
    """Fuzzes re-attached BatchState across 6,000 steps (seed_from_claims +
    count_missing_fill_as_unable + late claims).
    """

    def __init__(self) -> None:
        super().__init__()
        self.batch_id = "reattachbatch"
        self.state: BatchState | None = None
        self.ref: ReferenceBatchModel | None = None
        self.fill_slots: list[tuple[str, BatchGroup]] = []

    @initialize(
        groups=st.lists(
            st.tuples(
                st.sampled_from(["gpu-a", "gpu-b"]),
                st.integers(min_value=1, max_value=3),
                st.integers(min_value=1, max_value=3),
            ),
            min_size=1,
            max_size=2,
            unique_by=lambda t: t[0],
        ).map(
            lambda items: [
                BatchGroup(warmpool=pool, size=size, min_ready=min(min_r, size))
                for pool, size, min_r in items
            ]
        ),
        present_mask=st.integers(min_value=1, max_value=63),
    )
    def init_reattached_batch(self, groups: list[BatchGroup], present_mask: int) -> None:
        total_size = sum(g.size for g in groups)
        self.state = BatchState(self.batch_id, groups, next_ordinal=total_size)
        self.ref = ReferenceBatchModel(batch_id=self.batch_id, initial_next_ordinal=total_size)
        self.fill_slots = []
        seeded: list[dict[str, Any]] = []
        ordinal = 0
        for g in groups:
            for slot_in_group in range(g.size):
                cname = f"{self.batch_id}-{ordinal}"
                self.fill_slots.append((cname, g))
                # Always seed at least slot 0 per group so reconstruct_groups() would observe
                # the group's size and min_ready annotations during a real get_batch() attach.
                if slot_in_group == 0 or ((present_mask >> (ordinal % 6)) & 1):
                    seeded.append(
                        _make_claim_obj(
                            claim_name=cname,
                            warmpool=g.warmpool,
                            group_spec=g,
                            status_kind="ready",
                        )
                    )
                ordinal += 1
        self.state.seed_from_claims(seeded)
        self.state.count_missing_fill_as_unable()

    @precondition(lambda self: self.state is not None and len(self.fill_slots) > 0)
    @rule(mode=st.sampled_from([ConsumerMode.STREAM, ConsumerMode.GROUP, ConsumerMode.QUORUM]))
    def set_mode(self, mode: ConsumerMode) -> None:
        assert self.state is not None
        try:
            self.state.set_mode(mode)
        except BatchError:
            pass

    @precondition(lambda self: self.state is not None and len(self.fill_slots) > 0)
    @rule(
        idx=st.integers(min_value=0, max_value=10),
        status_kind=_STATUS_KINDS,
    )
    def late_or_updated_claim_event(self, idx: int, status_kind: str) -> None:
        assert self.state is not None
        cname, group = self.fill_slots[idx % len(self.fill_slots)]
        obj = _make_claim_obj(
            claim_name=cname,
            warmpool=group.warmpool,
            group_spec=group,
            status_kind=status_kind,
        )
        self.state.upsert_claim(obj)

    @precondition(lambda self: self.state is not None and len(self.fill_slots) > 0)
    @rule(idx=st.integers(min_value=0, max_value=10))
    def lose_claim(self, idx: int) -> None:
        assert self.state is not None
        cname, _ = self.fill_slots[idx % len(self.fill_slots)]
        self.state.mark_lost(cname)

    @invariant()
    def verify_reattach_invariants(self) -> None:
        if self.state is None or self.ref is None:
            return
        BatchInvariantOracle.assert_all(self.state, self.ref, in_flight_acquire_names=set())


class TestFullBatchStateFuzz(unittest.TestCase):
    """Deterministic Hypothesis state-machine test exercising all BatchState rules."""

    def test_full_batch_state_machine(self) -> None:
        runner = seed(1)(
            get_state_machine_test(
                FullBatchStateFuzzer,
                settings=settings(
                    max_examples=200,
                    stateful_step_count=50,
                    deadline=None,
                    derandomize=False,
                    database=None,
                ),
            )
        )
        runner()


TestHealthyCoreBatchStateFuzz = HealthyCoreBatchStateFuzzer.TestCase
TestReattachedBatchStateFuzz = ReattachedBatchStateFuzzer.TestCase


@settings(max_examples=40, deadline=None, derandomize=True)
@given(
    status_kind=st.sampled_from(["unreconciled", "cold_pending", "ready"]),
    reattached=st.booleans(),
)
def test_late_watch_added_after_release_not_ready_queues_pending_delete(
    status_kind: str, reattached: bool
) -> None:
    """INV-LEAK-1 / INV-OWN-4 (F-03): When a fill claim arrives on watch after
    `release_not_ready()` (normal watch lag after creator finished, or on `get_batch()`
    where no creator runs), `BatchState.upsert_claim` must record `needs_delete=True`
    so the claim is not tombstoned in `_released` while leaked live in Kubernetes.
    """
    group = BatchGroup(warmpool="gpu-a", size=2, min_ready=1)
    state = (
        BatchState("fuzzlate", [group], next_ordinal=2)
        if reattached
        else BatchState("fuzzlate", [group])
    )
    if reattached:
        state.seed_from_claims([
            _make_claim_obj(
                claim_name="fuzzlate-0",
                warmpool="gpu-a",
                group_spec=group,
                status_kind="ready",
            )
        ])
        state.count_missing_fill_as_unable()
    else:
        state.upsert_claim(
            _make_claim_obj(
                claim_name="fuzzlate-0",
                warmpool="gpu-a",
                group_spec=group,
                status_kind="ready",
            )
        )

    assert state.release_not_ready() == []
    late_obj = _make_claim_obj(
        claim_name="fuzzlate-1",
        warmpool="gpu-a",
        group_spec=group,
        status_kind=status_kind,
    )
    assert state.upsert_claim(late_obj) is None
    assert "fuzzlate-1" in state._released
    assert "fuzzlate-1" in state._pending_deletes
