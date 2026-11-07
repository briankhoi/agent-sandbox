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
Reusable invariant oracle and naive O(N) reference tracker for BatchState.

Provides structural and temporal assertions covering INV-OWN-4, INV-ACCT-1,
INV-ACCT-2, INV-ACCT-3, and INV-ACCT-4 across stateful Hypothesis fuzzing,
sync/async differential testing, and multi-worker concurrency stress suites.
"""

from collections import Counter
from dataclasses import dataclass, field

from k8s_agent_sandbox.batch_state import (
    BatchState,
    ConsumerMode,
    ordinal_sort_key,
    parse_ordinal,
)
from k8s_agent_sandbox.constants import COLD_CLAIM_READY_REASONS
from k8s_agent_sandbox.models import BatchEvent, BatchEventType, GroupOutcome, MemberState


@dataclass
class ReferenceBatchModel:
    """Tracks temporal history and stream outputs alongside a live BatchState."""

    batch_id: str
    initial_next_ordinal: int = 0
    high_water_ordinal: int = 0
    was_fill_settled: bool = False
    last_missing_total: int | None = None
    was_events_done: bool = False
    was_groups_done: bool = False
    ready_handed_out: set[str] = field(default_factory=set)
    quorum_cohort_recorded: bool = False
    group_cohorts_recorded: set[str] = field(default_factory=set)
    terminal_events_by_claim: dict[str, BatchEventType] = field(default_factory=dict)
    yielded_group_outcomes: dict[str, GroupOutcome] = field(default_factory=dict)
    last_delete_seq_by_claim: dict[str, int] = field(default_factory=dict)
    pool_unable_history: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.high_water_ordinal = self.initial_next_ordinal

    def drain_and_verify_streams(
        self, state: BatchState
    ) -> tuple[list[BatchEvent], list[GroupOutcome]]:
        """Pops queued events and group outcomes while checking INV-ACCT-4 uniqueness."""
        events: list[BatchEvent] = []
        while (ev := state.pop_event()) is not None:
            events.append(ev)
            cname = ev.member.claim_name
            if ev.type is BatchEventType.MEMBER_READY:
                assert cname not in state._released, (
                    f"INV-OWN-4 violated: pop_event() yielded MEMBER_READY for released "
                    f"claim {cname!r}"
                )
                assert cname not in self.ready_handed_out, (
                    f"INV-ACCT-4 violated: claim {cname!r} handed out more than once across "
                    f"MEMBER_READY / GroupOutcome / wait_for_quorum"
                )
                self.ready_handed_out.add(cname)
            elif ev.type in (BatchEventType.MEMBER_FAILED, BatchEventType.MEMBER_LOST):
                assert cname not in self.terminal_events_by_claim, (
                    f"INV-ACCT-4 violated: claim {cname!r} emitted duplicate terminal event "
                    f"{ev.type} after {self.terminal_events_by_claim[cname]}"
                )
                self.terminal_events_by_claim[cname] = ev.type

        outcomes: list[GroupOutcome] = []
        while (out := state.pop_group_outcome()) is not None:
            outcomes.append(out)
            assert out.warmpool not in self.yielded_group_outcomes, (
                f"INV-ACCT-4 violated: duplicate GroupOutcome yielded for pool {out.warmpool!r}"
            )
            self.yielded_group_outcomes[out.warmpool] = out
            if out.warmpool not in self.group_cohorts_recorded:
                self.group_cohorts_recorded.add(out.warmpool)
                for member in out.members:
                    assert member.claim_name not in self.ready_handed_out, (
                        f"INV-ACCT-4 violated: GroupOutcome cohort member {member.claim_name!r} "
                        f"already handed out earlier"
                    )
                    self.ready_handed_out.add(member.claim_name)

        if not self.quorum_cohort_recorded and state._quorum_members is not None:
            self.quorum_cohort_recorded = True
            for member in state._quorum_members:
                assert member.claim_name not in self.ready_handed_out, (
                    f"INV-ACCT-4 violated: quorum cohort member {member.claim_name!r} "
                    f"already handed out earlier"
                )
                self.ready_handed_out.add(member.claim_name)

        return events, outcomes

    def observe_step(self, state: BatchState, *, check_settled_monotonicity: bool = True) -> None:
        """Verifies temporal monotonicity invariants after any BatchState transition."""
        assert state.next_ordinal >= self.high_water_ordinal, (
            f"INV-ACCT-4 violated: next_ordinal regressed from {self.high_water_ordinal} "
            f"to {state.next_ordinal}"
        )
        self.high_water_ordinal = state.next_ordinal

        for cname, seq in state._pending_deletes.items():
            prev_seq = self.last_delete_seq_by_claim.get(cname, 0)
            assert seq >= prev_seq, (
                f"INV-OWN-4 violated: delete_seq for {cname!r} regressed from {prev_seq} to {seq}"
            )
            self.last_delete_seq_by_claim[cname] = seq

        current_missing_total = sum(state._missing.values())
        reopened_by_late_missing = (
            self.last_missing_total is not None and current_missing_total < self.last_missing_total
        )
        self.last_missing_total = current_missing_total
        if reopened_by_late_missing and not state.fill_settled():
            # Per test_a_missing_claim_that_arrives_later_takes_back_its_missing_count,
            # a late missing claim reopens fill_settled() so events() delivers its outcome.
            self.was_fill_settled = False

        if check_settled_monotonicity and self.was_fill_settled:
            assert state.fill_settled(), (
                "INV-ACCT-1 violated: fill_settled() regressed from True back to False "
                f"without a missing claim taking back its slot "
                f"(resolved={sorted(state._resolved)}, never_created={state._never_created}, "
                f"size={state.size}, fill_expired={state._fill_expired})"
            )
        if state.fill_settled():
            self.was_fill_settled = True

        if self.was_groups_done:
            assert state.groups_done(), (
                "INV-ACCT-3 violated: groups_done() regressed from True back to False"
            )
        if state.groups_done():
            self.was_groups_done = True


class BatchInvariantOracle:
    """Checks structural and reference-model invariants on BatchState."""

    @staticmethod
    def assert_structural_invariants(
        state: BatchState,
        *,
        check_dropped_ready_resolved: bool = True,
        in_flight_acquire_names: set[str] | None = None,
    ) -> None:
        """Asserts all O(N) structural invariants directly against BatchState internals."""
        # --- INV-OWN-4: Released tombstones and pending delete sequences ---
        overlap_members = state._released & state._members.keys()
        assert not overlap_members, (
            f"INV-OWN-4 violated: released claims still present in _members: "
            f"{sorted(overlap_members)}"
        )
        overlap_fill = state._released & state._fill_claims
        assert not overlap_fill, (
            f"INV-OWN-4 violated: released claims still present in _fill_claims: "
            f"{sorted(overlap_fill)}"
        )
        overlap_launch = state._released & state._pending_launch
        assert not overlap_launch, (
            f"INV-OWN-4 violated: released claims still present in _pending_launch: "
            f"{sorted(overlap_launch)}"
        )
        if in_flight_acquire_names is not None:
            leaked_acquiring = set(state._acquiring.keys()) - in_flight_acquire_names
            assert not leaked_acquiring, (
                f"INV-OWN-4 violated: finished/dropped acquire claims still present in _acquiring: "
                f"{sorted(leaked_acquiring)}"
            )
        for pool, waiting in state._waiting.items():
            overlap_waiting = state._released & waiting.keys()
            assert not overlap_waiting, (
                f"INV-OWN-4 violated: released claims still held in _waiting[{pool!r}]: "
                f"{sorted(overlap_waiting)}"
            )
        for cname, seq in state._pending_deletes.items():
            assert cname in state._released, (
                f"INV-OWN-4 violated: _pending_deletes contains non-released claim {cname!r}"
            )
            assert 1 <= seq <= state._next_delete_seq, (
                f"INV-OWN-4 violated: invalid delete seq {seq} for {cname!r} "
                f"(next_delete_seq={state._next_delete_seq})"
            )

        # --- INV-ACCT-1: Fill resolution & waiting-set partition ---
        expected_size = sum(g.size for g in state.groups)
        assert state.size == expected_size, (
            f"INV-ACCT-1 violated: state.size={state.size} != sum(g.size)={expected_size}"
        )
        assert state._unable <= state._resolved, (
            f"INV-ACCT-1 violated: _unable claims missing from _resolved: "
            f"{sorted(state._unable - state._resolved)}"
        )
        assert state._dispatched <= state._resolved, (
            f"INV-ACCT-1 violated: _dispatched claims missing from _resolved: "
            f"{sorted(state._dispatched - state._resolved)}"
        )
        for pool, waiting in state._waiting.items():
            wset = set(waiting.keys())
            assert wset.isdisjoint(state._resolved), (
                f"INV-ACCT-1 violated: _waiting[{pool!r}] overlaps _resolved: "
                f"{sorted(wset & state._resolved)}"
            )
            assert wset.isdisjoint(state._unable), (
                f"INV-ACCT-1 violated: _waiting[{pool!r}] overlaps _unable: "
                f"{sorted(wset & state._unable)}"
            )
            assert wset.isdisjoint(state._dispatched), (
                f"INV-ACCT-1 violated: _waiting[{pool!r}] overlaps _dispatched: "
                f"{sorted(wset & state._dispatched)}"
            )
            for cname in waiting:
                member = state._members.get(cname)
                assert (
                    member is not None
                    and member.state is MemberState.READY
                    and member.warmpool == pool
                ), (
                    f"INV-ACCT-1 violated: _waiting[{pool!r}] holds non-Ready or wrong-pool member "
                    f"{cname!r}: {member!r}"
                )

        if check_dropped_ready_resolved:
            for pool, outcome in state._group_outcomes.items():
                assert len(state._waiting[pool]) == 0, (
                    f"INV-ACCT-1 violated: decided group {pool!r} still has waiting members "
                    f"{list(state._waiting[pool])}"
                )
                if outcome.error is not None:
                    for cname, member in state._members.items():
                        if (
                            state._is_fill(cname, member)
                            and member.warmpool == pool
                            and member.state is MemberState.READY
                        ):
                            assert cname in state._resolved, (
                                f"INV-ACCT-1 violated: Ready fill member {cname!r} in "
                                f"failed/timed-out group {pool!r} ({type(outcome.error).__name__}) "
                                f"was dropped from _waiting[{pool!r}] without being added to "
                                f"_resolved (resolved={sorted(state._resolved)}, size={state.size})"
                            )

            if state._mode == ConsumerMode.QUORUM and (
                state._quorum_members is not None or state._quorum_error is not None
            ):
                for pool, waiting in state._waiting.items():
                    assert len(waiting) == 0, (
                        f"INV-ACCT-1 violated: decided quorum still has waiting members in {pool!r}"
                    )
                for cname, member in state._members.items():
                    if state._is_fill(cname, member) and member.state is MemberState.READY:
                        assert cname in state._resolved, (
                            f"INV-ACCT-1 violated: Ready fill member {cname!r} not in _resolved "
                            f"after quorum decided"
                        )

        if state._mode == ConsumerMode.STREAM:
            for pool, waiting in state._waiting.items():
                assert len(waiting) == 0, (
                    f"INV-ACCT-1 violated: STREAM mode retained waiting members in {pool!r}: "
                    f"{list(waiting)}"
                )

        # --- INV-ACCT-2: Incremental counters vs O(N) full scan ---
        for pool in state._group_by_pool:
            assert 0 <= state._missing[pool] <= state._lost[pool], (
                f"INV-ACCT-2 violated: invalid _missing[{pool!r}]={state._missing[pool]} "
                f"vs _lost[{pool!r}]={state._lost[pool]}"
            )
            assert state._failed[pool] >= 0 and state._released_count[pool] >= 0, (
                f"INV-ACCT-2 violated: negative unable counter for pool {pool!r}"
            )

        total_unable_counters = (
            sum(state._failed.values())
            + sum(state._lost.values())
            + sum(state._released_count.values())
        )
        expected_unable_total = len(state._unable) + sum(state._missing.values())
        assert total_unable_counters == expected_unable_total, (
            f"INV-ACCT-2 violated: sum(_failed + _lost + _released_count)={total_unable_counters} "
            f"!= len(_unable) + sum(_missing)={expected_unable_total}"
        )

        for cname in state._pending_launch:
            member = state._members.get(cname)
            assert (
                member is not None
                and member.state is MemberState.PENDING
                and member.reason not in COLD_CLAIM_READY_REASONS
            ), (
                f"INV-ACCT-2 violated: settled or missing claim {cname!r} lingered in "
                f"_pending_launch (member={member!r})"
            )

        for pool, cnt in state._checking_acquires.items():
            assert cnt > 0, f"INV-ACCT-2 violated: non-positive _checking_acquires[{pool!r}]={cnt}"

        assert state.next_ordinal >= state.size, (
            f"INV-ACCT-2 violated: next_ordinal={state.next_ordinal} < size={state.size}"
        )
        for cname in set(state._members.keys()) | set(state._acquiring.keys()):
            ord_val = parse_ordinal(state.batch_id, cname)
            if ord_val is not None:
                assert state.next_ordinal > ord_val, (
                    f"INV-ACCT-2 violated: next_ordinal={state.next_ordinal} <= active ordinal "
                    f"{ord_val} ({cname!r})"
                )

        # Verify members() ordering matches ordinal_sort_key.
        listed = state.members()
        listed_keys = [
            ordinal_sort_key(parse_ordinal(state.batch_id, m.claim_name)) for m in listed
        ]
        assert listed_keys == sorted(listed_keys), (
            f"INV-ACCT-2 violated: members() is not sorted by ordinal: "
            f"{[m.claim_name for m in listed]}"
        )
        assert len(listed) == len(state._members), (
            f"INV-ACCT-2 violated: len(members())={len(listed)} != "
            f"len(_members)={len(state._members)}"
        )

        # --- INV-ACCT-3: Consumer mode, quorum, and group outcome invariants ---
        assert not (state._quorum_members is not None and state._quorum_error is not None), (
            "INV-ACCT-3 violated: _quorum_members and _quorum_error are both non-None"
        )
        if state._quorum_members is not None:
            by_pool = Counter(m.warmpool for m in state._quorum_members)
            expected_by_pool = {
                p: state._min_ready[p] for p in state._group_by_pool if state._min_ready[p] > 0
            }
            assert dict(by_pool) == expected_by_pool, (
                f"INV-ACCT-3 violated: quorum cohort counts {dict(by_pool)} != "
                f"expected min_ready {expected_by_pool}"
            )

        if (
            state._mode == ConsumerMode.QUORUM
            and state._quorum_members is None
            and state._quorum_error is None
        ):
            expected_short = {
                p for p in state._group_by_pool if len(state._waiting[p]) < state._min_ready[p]
            }
            assert state._groups_short_of_quorum == expected_short, (
                f"INV-ACCT-3 violated: _groups_short_of_quorum={state._groups_short_of_quorum} "
                f"!= actual short pools {expected_short}"
            )
            assert len(expected_short) > 0, (
                "INV-ACCT-3 violated: no group short of quorum yet quorum remains undecided"
            )
            for p, group in state._group_by_pool.items():
                unable_p = state._failed[p] + state._lost[p] + state._released_count[p]
                assert group.size - unable_p >= state._min_ready[p], (
                    f"INV-ACCT-3 violated: pool {p!r} cannot reach min_ready={state._min_ready[p]} "
                    f"(size={group.size}, unable={unable_p}) yet quorum did not fail"
                )

        for pool, outcome in state._group_outcomes.items():
            if outcome.error is None:
                assert len(outcome.members) == state._min_ready[pool], (
                    f"INV-ACCT-3 violated: GroupOutcome({pool!r}) has {len(outcome.members)} "
                    f"members, expected min_ready={state._min_ready[pool]}"
                )
                assert all(m.warmpool == pool for m in outcome.members), (
                    f"INV-ACCT-3 violated: GroupOutcome({pool!r}) contains wrong-pool member"
                )
            else:
                assert len(outcome.members) == 0, (
                    f"INV-ACCT-3 violated: failed GroupOutcome({pool!r}) returned non-empty members"
                )

    @classmethod
    def assert_all(
        cls,
        state: BatchState,
        ref: ReferenceBatchModel,
        *,
        check_dropped_ready_resolved: bool = True,
        check_settled_monotonicity: bool = True,
        in_flight_acquire_names: set[str] | None = None,
    ) -> tuple[list[BatchEvent], list[GroupOutcome]]:
        """Runs full structural + reference stream/monotonicity checks in one call."""
        cls.assert_structural_invariants(
            state,
            check_dropped_ready_resolved=check_dropped_ready_resolved,
            in_flight_acquire_names=in_flight_acquire_names,
        )
        events, outcomes = ref.drain_and_verify_streams(state)
        ref.observe_step(state, check_settled_monotonicity=check_settled_monotonicity)
        return events, outcomes
