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

import random
import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from urllib3 import HTTPHeaderDict

from k8s_agent_sandbox import batch_state
from k8s_agent_sandbox.batch_state import (
    BATCH_CREATE_BACKOFF_MAX_SECONDS,
    BATCH_CREATE_RETRY_AFTER_MAX_SECONDS,
    BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    CLOCK_SKEW_MARGIN,
)
from k8s_agent_sandbox.exceptions import BatchError, QuorumUnreachableError
from k8s_agent_sandbox.models import BatchEventType, BatchGroup
from k8s_agent_sandbox.constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    TERMINAL_CLAIM_READY_REASONS,
)


def _claim(
    name: str,
    warmpool: str = "pool-a",
    conditions: list[dict] | None = None,
    sandbox: dict | None = None,
    annotations: dict | None = None,
    deletion_timestamp: str | None = None,
) -> dict:
    metadata: dict = {"name": name}
    if annotations:
        metadata["annotations"] = annotations
    if deletion_timestamp:
        metadata["deletionTimestamp"] = deletion_timestamp
    status: dict = {}
    if conditions is not None:
        status["conditions"] = conditions
    if sandbox is not None:
        status["sandbox"] = sandbox
    return {
        "metadata": metadata,
        "spec": {"warmPoolRef": {"name": warmpool}},
        "status": status,
    }


class TestDeriveMember(unittest.TestCase):
    """The batch state must derive a Member correctly from a SandboxClaim."""

    def test_pending_no_sandbox_name(self):
        member = batch_state.derive_member(_claim("b1-0", conditions=[]))
        self.assertFalse(member.ready)
        self.assertIsNone(member.sandbox_name)
        self.assertFalse(member.terminal)

    def test_bound_but_not_ready(self):
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "False", "reason": "SandboxNotReady"}],
                sandbox={"name": "sbx-1"},
            )
        )
        self.assertFalse(member.ready)
        self.assertEqual(member.sandbox_name, "sbx-1")
        self.assertFalse(member.terminal)
        self.assertEqual(member.reason, "SandboxNotReady")

    def test_ready(self):
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True"}],
                sandbox={"name": "sbx-1", "podIPs": ["10.0.0.1"], "serviceFQDN": "sbx-1.svc"},
            )
        )
        self.assertTrue(member.ready)
        self.assertEqual(member.pod_ips, ("10.0.0.1",))
        self.assertEqual(member.service_fqdn, "sbx-1.svc")

    def test_ready_without_sandbox_name_is_not_ready(self):
        member = batch_state.derive_member(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}])
        )
        self.assertFalse(member.ready)

    def test_terminal_claim_ready_reasons(self):
        for reason in TERMINAL_CLAIM_READY_REASONS:
            with self.subTest(reason=reason):
                member = batch_state.derive_member(
                    _claim(
                        "b1-0",
                        conditions=[
                            {"type": "Ready", "status": "False", "reason": reason, "message": "x"}
                        ],
                    )
                )
                self.assertTrue(member.terminal)
                self.assertEqual(member.reason, reason)

    def test_template_not_found_is_not_terminal(self):
        # The controller requeues TemplateNotFound every minute rather than failing the claim.
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[
                    {"type": "Ready", "status": "False", "reason": "TemplateNotFound", "message": "x"}
                ],
            )
        )
        self.assertFalse(member.terminal)
        self.assertFalse(member.ready)
        self.assertEqual(member.reason, "TemplateNotFound")
        self.assertEqual(member.message, "x")

    def test_warmpool_not_found_is_not_terminal(self):
        # The controller requeues WarmPoolNotFound every minute rather than failing the claim.
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[
                    {"type": "Ready", "status": "False", "reason": "WarmPoolNotFound", "message": "no pool"}
                ],
            )
        )
        self.assertFalse(member.terminal)
        self.assertFalse(member.ready)
        self.assertEqual(member.reason, "WarmPoolNotFound")
        self.assertEqual(member.message, "no pool")

    def test_pod_ips_and_service_fqdn_mapping(self):
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True"}],
                sandbox={"name": "sbx-1", "podIPs": ["10.0.0.1", "10.0.0.2"], "serviceFQDN": "sbx-1.default.svc"},
            )
        )
        self.assertEqual(member.pod_ips, ("10.0.0.1", "10.0.0.2"))
        self.assertEqual(member.service_fqdn, "sbx-1.default.svc")


class TestLostVsReleased(unittest.TestCase):

    def test_deleted_unreleased_claim_sets_lost(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        lost = state.mark_lost("b1-0")
        self.assertIsNotNone(lost)
        self.assertTrue(lost.lost)
        self.assertTrue(state.get_member("b1-0").lost)

    def test_lost_member_clears_ready_and_endpoints(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.upsert_claim(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True", "reason": "Ready"}],
                sandbox={"name": "sbx-1", "podIPs": ["10.0.0.1"], "serviceFQDN": "sbx-1.svc"},
            )
        )
        lost = state.mark_lost("b1-0")
        self.assertFalse(lost.ready)
        self.assertEqual(lost.pod_ips, ())
        self.assertIsNone(lost.service_fqdn)
        self.assertEqual(lost.reason, "Ready")

    def test_deleted_released_claim_is_not_marked_lost(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        state.mark_released("b1-0")
        result = state.mark_lost("b1-0")
        self.assertIsNone(result)
        self.assertIsNone(state.get_member("b1-0"))
        self.assertNotIn("b1-0", [m.claim_name for m in state.members()])


class TestBatchGroup(unittest.TestCase):

    def test_min_ready_defaults_to_size(self):
        self.assertEqual(BatchGroup(warmpool="p", size=3).min_ready, 3)

    def test_min_ready_greater_than_size_raises(self):
        with self.assertRaises(ValidationError):
            BatchGroup(warmpool="p", size=3, min_ready=4)

    def test_group_is_frozen(self):
        group = BatchGroup(warmpool="p", size=3)
        with self.assertRaises(ValidationError):
            group.min_ready = 1


class TestReconstructGroups(unittest.TestCase):

    def test_annotated_groups_come_back_with_size_and_min_ready(self):
        items = [
            _claim(
                "b1-0",
                annotations={
                    "agents.x-k8s.io/batch-group-size": "3",
                    "agents.x-k8s.io/batch-group-min-ready": "2",
                },
            ),
        ]
        groups = batch_state.reconstruct_groups(items)
        self.assertEqual(groups, [BatchGroup(warmpool="pool-a", size=3, min_ready=2)])

    def test_unannotated_pool_becomes_size_zero(self):
        items = [_claim("b1-0", warmpool="pool-b")]
        groups = batch_state.reconstruct_groups(items)
        self.assertEqual(groups, [BatchGroup(warmpool="pool-b", size=0, min_ready=0)])

    def test_conflicting_annotations_raise(self):
        items = [
            _claim(
                "b1-0",
                annotations={
                    "agents.x-k8s.io/batch-group-size": "3",
                    "agents.x-k8s.io/batch-group-min-ready": "2",
                },
            ),
            _claim(
                "b1-1",
                annotations={
                    "agents.x-k8s.io/batch-group-size": "5",
                    "agents.x-k8s.io/batch-group-min-ready": "2",
                },
            ),
        ]
        with self.assertRaises(BatchError):
            batch_state.reconstruct_groups(items)

    def test_partial_annotations_raise(self):
        items = [_claim("b1-0", annotations={"agents.x-k8s.io/batch-group-size": "3"})]
        with self.assertRaises(BatchError):
            batch_state.reconstruct_groups(items)

    def test_min_ready_greater_than_size_raises(self):
        items = [
            _claim(
                "b1-0",
                annotations={
                    "agents.x-k8s.io/batch-group-size": "2",
                    "agents.x-k8s.io/batch-group-min-ready": "5",
                },
            ),
        ]
        with self.assertRaises(ValidationError):
            batch_state.reconstruct_groups(items)


class TestOrdinalAllocation(unittest.TestCase):

    def test_ordinal_counter_handles_gaps_and_terminating_claims(self):
        items = [
            _claim("b1-0"),
            _claim("b1-3"),
            _claim("b1-5",  deletion_timestamp="2026-01-01T00:00:00Z"),
        ]
        self.assertEqual(batch_state.compute_next_ordinal("b1", items), 6)

    def test_initial_fill_holds_ordinals_below_group_size(self):
        groups = [BatchGroup(warmpool="pool-a", size=2)]
        state = batch_state.BatchState("b1", groups)
        items = [_claim("b1-0"), _claim("b1-1"), _claim("b1-2")]
        state.seed_from_claims(items)
        self.assertIn("b1-0", state._initial_fill)
        self.assertIn("b1-1", state._initial_fill)
        self.assertNotIn("b1-2", state._initial_fill)


class TestSnapshots(unittest.TestCase):

    def test_get_member_snapshot_unaffected_by_later_upsert_claim(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.upsert_claim(_claim("b1-0", conditions=[]))
        first = state.get_member("b1-0")
        state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.assertFalse(first.ready)
        second = state.get_member("b1-0")
        self.assertTrue(second.ready)

    def test_members_pod_ips_cannot_be_mutated(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.upsert_claim(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True"}],
                sandbox={"name": "sbx-1", "podIPs": ["10.0.0.1"]},
            )
        )
        member = state.members()[0]
        self.assertEqual(member.pod_ips, ("10.0.0.1",))
        with self.assertRaises(AttributeError):
            member.pod_ips.append("10.0.0.99")


class TestDispatchedSet(unittest.TestCase):

    def test_try_dispatch_is_at_most_once_per_claim(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        self.assertTrue(state.try_dispatch("b1-0"))
        self.assertFalse(state.try_dispatch("b1-0"))
        self.assertTrue(state.try_dispatch("b1-1"))


class TestErrorSticky(unittest.TestCase):

    def test_first_error_wins(self):
        state = batch_state.BatchState("b1", [])
        state.note_error(ValueError("first"))
        state.note_error(ValueError("second"))
        self.assertEqual(str(state.error()), "first")


class TestLeaseStaleness(unittest.TestCase):

    def test_boundary_exactly_at_skew_margin_is_not_stale(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        renew_time = now - timedelta(
            seconds=BATCH_DEFAULT_LEASE_DURATION_SECONDS - CLOCK_SKEW_MARGIN
        )
        self.assertFalse(
            batch_state.is_lease_stale(renew_time, BATCH_DEFAULT_LEASE_DURATION_SECONDS, now)
        )

    def test_one_second_after_margin_is_stale(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        renew_time = now - timedelta(
            seconds=BATCH_DEFAULT_LEASE_DURATION_SECONDS - CLOCK_SKEW_MARGIN + 1
        )
        self.assertTrue(
            batch_state.is_lease_stale(renew_time, BATCH_DEFAULT_LEASE_DURATION_SECONDS, now)
        )


if __name__ == "__main__":
    unittest.main()


_READY = [{"type": "Ready", "status": "True"}]


def _ready(name, warmpool="pool-a"):
    return _claim(name, warmpool=warmpool, conditions=_READY, sandbox={"name": f"sbx-{name}"})


def _terminal(name, warmpool="pool-a", reason="InvalidMetadata"):
    return _claim(
        name, warmpool=warmpool, conditions=[{"type": "Ready", "status": "False", "reason": reason}]
    )


def _planned_state(groups, batch_id="b1"):
    state = batch_state.BatchState(batch_id, groups)
    state.plan_initial_fill()
    state.set_fill_deadline(600.0)
    return state


def _drain(state):
    events, done = state.collect_events()
    return [(e.type, e.member.claim_name if e.member else None) for e in events], done


class TestCreateRetryClassification(unittest.TestCase):

    def test_table(self):
        C = batch_state.CreateOutcome
        cases = [
            (429, 1, C.RETRY),
            (503, 1, C.RETRY),
            (500, 2, C.RETRY),
            (None, 1, C.RETRY),
            (429, 3, C.FAIL),
            (503, 3, C.FAIL),
            (409, 1, C.FAIL),
            (409, 2, C.SUCCESS),
            (409, 3, C.SUCCESS),
            (400, 1, C.FAIL),
            (403, 1, C.FAIL),
            (404, 1, C.FAIL),
            (422, 1, C.FAIL),
        ]
        for status, attempt, expected in cases:
            with self.subTest(status=status, attempt=attempt):
                self.assertIs(batch_state.classify_create_error(status, attempt), expected)

    def test_retry_after_parsing(self):
        self.assertEqual(batch_state.parse_retry_after({"Retry-After": "3"}), 3.0)
        self.assertEqual(batch_state.parse_retry_after(HTTPHeaderDict({"retry-after": "1.5"})), 1.5)
        self.assertIsNone(batch_state.parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}))
        self.assertIsNone(batch_state.parse_retry_after({}))
        self.assertIsNone(batch_state.parse_retry_after(None))
        self.assertEqual(
            batch_state.parse_retry_after({"Retry-After": "3600"}), BATCH_CREATE_RETRY_AFTER_MAX_SECONDS
        )

    def test_backoff_honors_retry_after_and_jitters_exponentially(self):
        self.assertEqual(batch_state.create_backoff_delay(1, 7.0, rand=lambda: 0.9), 7.0)
        low = [batch_state.create_backoff_delay(a, None, rand=lambda: 0.0) for a in (1, 2, 3)]
        high = [batch_state.create_backoff_delay(a, None, rand=lambda: 1.0) for a in (1, 2, 3)]
        self.assertEqual(low, [0.25, 0.5, 1.0])
        self.assertEqual(high, [0.5, 1.0, 2.0])
        self.assertLessEqual(
            batch_state.create_backoff_delay(30, None, rand=lambda: 1.0), BATCH_CREATE_BACKOFF_MAX_SECONDS
        )


class TestCreatePacer(unittest.TestCase):

    def test_no_more_than_rate_starts_in_any_one_second(self):
        pacer = batch_state.CreatePacer(50.0)
        now = 0.0
        starts = []
        for _ in range(500):
            start = pacer.reserve(now)
            starts.append(start)
            now = start  # the caller sleeps until its slot
        for i, first in enumerate(starts):
            # The epsilon keeps float rounding in first + 1.0 from widening the half-open window.
            in_window = [t for t in starts[i:] if t < first + 1.0 - 1e-9]
            self.assertLessEqual(len(in_window), 50)
        self.assertAlmostEqual(starts[-1], 499 / 50.0)

    def test_idle_time_does_not_bank_a_burst(self):
        pacer = batch_state.CreatePacer(10.0)
        self.assertEqual(pacer.reserve(0.0), 0.0)
        self.assertEqual(pacer.reserve(100.0), 100.0)
        self.assertAlmostEqual(pacer.reserve(100.0), 100.1)


class TestPlanInitialFill(unittest.TestCase):

    def test_ordinals_assigned_across_groups_in_argument_order(self):
        state = batch_state.BatchState(
            "b1", [BatchGroup(warmpool="pool-z", size=2), BatchGroup(warmpool="pool-a", size=3)]
        )
        plan = state.plan_initial_fill()
        self.assertEqual(
            [(name, g.warmpool) for name, g in plan],
            [
                ("b1-0", "pool-z"),
                ("b1-1", "pool-z"),
                ("b1-2", "pool-a"),
                ("b1-3", "pool-a"),
                ("b1-4", "pool-a"),
            ],
        )
        self.assertEqual(state._initial_fill, {f"b1-{i}" for i in range(5)})
        self.assertEqual(state._next_ordinal, 5)
        self.assertFalse(state.is_settled())


class TestReconstructGroupOrder(unittest.TestCase):

    def test_groups_ordered_by_lowest_ordinal_then_unparseable_by_name(self):
        def annotated(name, pool, size):
            return _claim(
                name,
                warmpool=pool,
                annotations={BATCH_GROUP_SIZE_ANNOTATION: str(size), BATCH_GROUP_MIN_READY_ANNOTATION: "1"},
            )

        items = [
            annotated("b1-3", "pool-a", 2),
            annotated("b1-0", "pool-z", 3),
            annotated("b1-4", "pool-a", 2),
            _claim("stray-x", warmpool="pool-c"),
            _claim("stray-y", warmpool="pool-b"),
        ]
        groups = batch_state.reconstruct_groups(items, "b1")
        self.assertEqual([g.warmpool for g in groups], ["pool-z", "pool-a", "pool-b", "pool-c"])


class TestGroupQuorum(unittest.TestCase):

    def test_group_yields_exactly_min_ready_lowest_ordinals_first(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=4, min_ready=2)])
        state.claim_groups_consumer()
        state.upsert_claim(_ready("b1-3"))
        self.assertEqual(state.pop_group_verdicts(), ([], False))
        state.upsert_claim(_ready("b1-1"))
        state.upsert_claim(_ready("b1-0"))
        verdicts, done = state.pop_group_verdicts()
        self.assertTrue(done)
        self.assertEqual([m.claim_name for m in verdicts[0].members], ["b1-1", "b1-3"])
        self.assertIsNone(verdicts[0].error)

    def test_fast_group_yields_before_slow_group(self):
        state = _planned_state(
            [BatchGroup(warmpool="slow", size=2), BatchGroup(warmpool="fast", size=1)]
        )
        state.claim_groups_consumer()
        state.upsert_claim(_ready("b1-2", warmpool="fast"))
        verdicts, done = state.pop_group_verdicts()
        self.assertEqual([v.warmpool for v in verdicts], ["fast"])
        self.assertFalse(done)
        state.upsert_claim(_ready("b1-0", warmpool="slow"))
        state.upsert_claim(_ready("b1-1", warmpool="slow"))
        verdicts, done = state.pop_group_verdicts()
        self.assertEqual([v.warmpool for v in verdicts], ["slow"])
        self.assertTrue(done)

    def test_unreachable_group_errors_while_other_group_yields(self):
        state = _planned_state(
            [BatchGroup(warmpool="bad", size=3, min_ready=2), BatchGroup(warmpool="good", size=1)]
        )
        state.claim_groups_consumer()
        state.upsert_claim(_terminal("b1-0", warmpool="bad"))
        self.assertEqual(state.pop_group_verdicts()[0], [])
        state.upsert_claim(_claim("b1-1", warmpool="bad"))
        state.mark_lost("b1-1")
        state.upsert_claim(_ready("b1-3", warmpool="good"))
        verdicts, done = state.pop_group_verdicts()
        by_pool = {v.warmpool: v for v in verdicts}
        self.assertTrue(done)
        error = by_pool["bad"].error
        self.assertIsInstance(error, QuorumUnreachableError)
        self.assertEqual(by_pool["bad"].members, [])
        self.assertEqual(
            (error.warmpool, error.size, error.min_ready, error.terminal, error.lost), ("bad", 3, 2, 1, 1)
        )
        self.assertEqual([m.claim_name for m in by_pool["good"].members], ["b1-3"])

    def test_released_members_count_as_unable_to_arrive(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=2)])
        state.claim_groups_consumer()
        state.mark_released("b1-0")
        verdicts, _ = state.pop_group_verdicts()
        self.assertIsInstance(verdicts[0].error, QuorumUnreachableError)
        self.assertEqual(verdicts[0].error.released, 1)

    def test_min_ready_zero_yields_immediately_with_no_members(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=2, min_ready=0)])
        state.claim_groups_consumer()
        verdicts, done = state.pop_group_verdicts()
        self.assertEqual(verdicts[0].members, [])
        self.assertIsNone(verdicts[0].error)
        self.assertTrue(done)

    def test_group_times_out_at_deadline_without_affecting_others(self):
        state = _planned_state(
            [BatchGroup(warmpool="stuck", size=2), BatchGroup(warmpool="ok", size=1)]
        )
        state.claim_groups_consumer()
        state.upsert_claim(_ready("b1-2", warmpool="ok"))
        state.upsert_claim(_ready("b1-0", warmpool="stuck"))
        verdicts, _ = state.pop_group_verdicts()
        self.assertEqual([v.warmpool for v in verdicts], ["ok"])
        self.assertFalse(state.check_deadline(599.0))
        self.assertTrue(state.check_deadline(600.0))
        verdicts, done = state.pop_group_verdicts()
        self.assertTrue(done)
        self.assertEqual(verdicts[0].warmpool, "stuck")
        self.assertIsInstance(verdicts[0].error, TimeoutError)
        self.assertEqual(str(verdicts[0].error), "Group quorum timed out")

    def test_dependency_not_found_reasons_are_pending_not_terminal(self):
        for reason in ("WarmPoolNotFound", "TemplateNotFound"):
            with self.subTest(reason=reason):
                state = _planned_state([BatchGroup(warmpool="pool-a", size=2)])
                state.claim_events_consumer()
                state.upsert_claim(_terminal("b1-0", reason=reason))
                state.upsert_claim(_ready("b1-1"))
                self.assertEqual(state._group_fills["pool-a"].reachable(), 2)
                events, done = _drain(state)
                self.assertEqual(events, [(BatchEventType.MEMBER_READY, "b1-1")])
                self.assertFalse(done)
                state.check_deadline(600.0)
                self.assertEqual(_drain(state), ([], True))

    def test_create_failure_fail_fast_threshold(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=4, min_ready=3)])
        state.claim_groups_consumer()
        self.assertFalse(state.mark_create_failed("b1-0", "boom"))
        self.assertEqual(state.pop_group_verdicts()[0], [])
        self.assertTrue(state.mark_create_failed("b1-1", "boom"))
        verdicts, _ = state.pop_group_verdicts()
        self.assertIsInstance(verdicts[0].error, QuorumUnreachableError)
        self.assertEqual(verdicts[0].error.create_failed, 2)
        # Past the threshold every further failure keeps asking; cancelling is idempotent.
        self.assertTrue(state.mark_create_failed("b1-2", "boom"))
        member = state.get_member("b1-0")
        self.assertTrue(member.terminal)
        self.assertEqual(member.reason, "CreateFailed")
        self.assertEqual(member.message, "boom")

    def test_create_failures_never_ask_to_cancel_outside_quorum_mode(self):
        for claim_events in (False, True):
            with self.subTest(stream_mode=claim_events):
                state = _planned_state([BatchGroup(warmpool="pool-a", size=3)])
                if claim_events:
                    state.claim_events_consumer()
                self.assertFalse(state.mark_create_failed("b1-0", "boom"))
                self.assertFalse(state.mark_create_failed("b1-1", "boom"))

    def test_quorum_mode_cancels_groups_already_past_threshold(self):
        state = _planned_state(
            [BatchGroup(warmpool="pool-a", size=4, min_ready=3), BatchGroup(warmpool="pool-b", size=2)]
        )
        state.mark_create_failed("b1-0", "boom")
        state.mark_create_failed("b1-1", "boom")
        self.assertEqual(state.claim_groups_consumer(), ["pool-a"])
        verdicts, _ = state.pop_group_verdicts()
        self.assertEqual([v.warmpool for v in verdicts], ["pool-a"])
        self.assertIsInstance(verdicts[0].error, QuorumUnreachableError)

    def test_quorum_mode_with_no_failures_cancels_nothing(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=2)])
        self.assertEqual(state.claim_groups_consumer(), [])

    def test_create_failed_member_ignores_later_watch_updates(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=1)])
        state.mark_create_failed("b1-0", "boom")
        state.upsert_claim(_ready("b1-0"))
        self.assertTrue(state.get_member("b1-0").terminal)

    def test_second_groups_consumer_raises(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=1)])
        state.claim_groups_consumer()
        with self.assertRaises(BatchError):
            state.claim_groups_consumer()


class TestConsumerMode(unittest.TestCase):

    def test_events_first_makes_groups_consumer_raise(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=1)])
        state.claim_events_consumer()
        with self.assertRaises(BatchError):
            state.claim_groups_consumer()

    def test_second_events_consumer_raises(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=1)])
        state.claim_events_consumer()
        with self.assertRaises(BatchError):
            state.claim_events_consumer()

    def test_quorum_first_holds_back_min_ready_and_streams_extras(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=4, min_ready=2)])
        state.claim_groups_consumer()
        state.claim_events_consumer()
        state.upsert_claim(_ready("b1-0"))
        self.assertEqual(_drain(state), ([], False))
        state.upsert_claim(_ready("b1-1"))
        verdicts, _ = state.pop_group_verdicts()
        self.assertEqual([m.claim_name for m in verdicts[0].members], ["b1-0", "b1-1"])
        self.assertEqual(_drain(state), ([], False))
        state.upsert_claim(_ready("b1-2"))
        self.assertEqual(_drain(state), ([(BatchEventType.MEMBER_READY, "b1-2")], False))
        state.upsert_claim(_ready("b1-3"))
        self.assertEqual(_drain(state), ([(BatchEventType.MEMBER_READY, "b1-3")], True))

    def test_error_verdict_releases_held_members_to_events(self):
        for cause in ("unreachable", "timeout"):
            with self.subTest(cause=cause):
                state = _planned_state([BatchGroup(warmpool="pool-a", size=3, min_ready=3)])
                state.claim_groups_consumer()
                state.claim_events_consumer()
                state.upsert_claim(_ready("b1-1"))
                state.upsert_claim(_ready("b1-0"))
                self.assertEqual(_drain(state), ([], False))
                if cause == "unreachable":
                    state.upsert_claim(_terminal("b1-2"))
                else:
                    state.check_deadline(600.0)
                verdicts, _ = state.pop_group_verdicts()
                self.assertIsNotNone(verdicts[0].error)
                events, done = _drain(state)
                ready = [name for kind, name in events if kind is BatchEventType.MEMBER_READY]
                self.assertEqual(ready, ["b1-0", "b1-1"])
                self.assertTrue(done)


class TestEventStream(unittest.TestCase):

    def test_ordering_and_failure_and_lost_events(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=4)])
        state.claim_events_consumer()
        state.upsert_claim(_ready("b1-2"))
        state.upsert_claim(_terminal("b1-1"))
        state.upsert_claim(_ready("b1-0"))
        state.mark_create_failed("b1-3", "quota exceeded")
        events, done = _drain(state)
        self.assertEqual(
            events,
            [
                (BatchEventType.MEMBER_READY, "b1-2"),
                (BatchEventType.MEMBER_FAILED, "b1-1"),
                (BatchEventType.MEMBER_READY, "b1-0"),
                (BatchEventType.MEMBER_FAILED, "b1-3"),
            ],
        )
        self.assertTrue(done)

    def test_lost_member_emits_member_lost_after_ready(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=2)])
        state.claim_events_consumer()
        state.upsert_claim(_ready("b1-0"))
        self.assertEqual(_drain(state)[0], [(BatchEventType.MEMBER_READY, "b1-0")])
        state.mark_lost("b1-0")
        self.assertEqual(_drain(state), ([(BatchEventType.MEMBER_LOST, "b1-0")], False))

    def test_only_initial_fill_members_produce_events(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=1)])
        state.claim_events_consumer()
        state.upsert_claim(_ready("b1-5"))
        state.upsert_claim(_terminal("b1-6"))
        state.upsert_claim(_ready("b1-0"))
        events, _ = _drain(state)
        self.assertEqual(events, [(BatchEventType.MEMBER_READY, "b1-0")])

    def test_settles_on_mixed_outcome(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=5)])
        state.claim_events_consumer()
        state.upsert_claim(_ready("b1-0"))
        state.upsert_claim(_ready("b1-1"))
        state.upsert_claim(_terminal("b1-2"))
        state.mark_create_failed("b1-3", "boom")
        self.assertFalse(_drain(state)[1])
        state.mark_released("b1-4")
        self.assertEqual(_drain(state), ([], True))

    def test_settles_at_deadline_with_member_pending_forever(self):
        for mode in ("stream", "quorum"):
            with self.subTest(mode=mode):
                state = _planned_state([BatchGroup(warmpool="pool-a", size=2, min_ready=1)])
                if mode == "quorum":
                    state.claim_groups_consumer()
                state.claim_events_consumer()
                state.upsert_claim(_ready("b1-0"))
                state.upsert_claim(_claim("b1-1"))
                if mode == "quorum":
                    self.assertEqual(len(state.pop_group_verdicts()[0]), 1)
                self.assertFalse(_drain(state)[1])
                self.assertTrue(state.check_deadline(600.0))
                self.assertTrue(_drain(state)[1])

    def test_lease_degraded_event(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=1)])
        state.claim_events_consumer()
        state.note_lease_degraded()
        self.assertEqual(_drain(state), ([(BatchEventType.LEASE_DEGRADED, None)], False))

    def test_closed_ends_stream_and_raises_for_groups(self):
        state = _planned_state([BatchGroup(warmpool="pool-a", size=1)])
        state.claim_groups_consumer()
        state.claim_events_consumer()
        state.close()
        self.assertEqual(state.collect_events(), ([], True))
        with self.assertRaises(BatchError):
            state.pop_group_verdicts()
        with self.assertRaises(BatchError):
            state.claim_events_consumer()


class TestReattachedState(unittest.TestCase):

    def _seeded(self, items, groups):
        state = batch_state.BatchState("b1", groups)
        state.seed_from_claims(items)
        state.set_fill_deadline(600.0)
        return state

    def test_quorum_already_met_yields_on_claim_in_ordinal_order(self):
        group = BatchGroup(warmpool="pool-a", size=3, min_ready=2)
        state = self._seeded([_ready("b1-2"), _ready("b1-0"), _ready("b1-1")], [group])
        state.claim_groups_consumer()
        state.claim_events_consumer()
        verdicts, done = state.pop_group_verdicts()
        self.assertTrue(done)
        self.assertEqual([m.claim_name for m in verdicts[0].members], ["b1-0", "b1-1"])
        self.assertEqual(_drain(state), ([(BatchEventType.MEMBER_READY, "b1-2")], True))

    def test_stream_emits_already_ready_members_in_ordinal_order(self):
        group = BatchGroup(warmpool="pool-a", size=3)
        state = self._seeded([_ready("b1-2"), _ready("b1-0"), _ready("b1-1")], [group])
        state.claim_events_consumer()
        events, done = _drain(state)
        self.assertEqual([name for _, name in events], ["b1-0", "b1-1", "b1-2"])
        self.assertTrue(done)

    def test_claims_missing_at_attach_count_as_lost(self):
        group = BatchGroup(warmpool="pool-a", size=3, min_ready=3)
        state = self._seeded([_ready("b1-0"), _ready("b1-1")], [group])
        state.claim_groups_consumer()
        verdicts, _ = state.pop_group_verdicts()
        self.assertIsInstance(verdicts[0].error, QuorumUnreachableError)
        self.assertEqual(verdicts[0].error.lost, 1)


class TestDispatchInterleavings(unittest.TestCase):

    def test_no_member_handed_out_twice_across_consumers(self):
        for seed in range(200):
            rng = random.Random(seed)
            groups = [
                BatchGroup(warmpool="pool-a", size=4, min_ready=rng.randint(0, 4)),
                BatchGroup(warmpool="pool-b", size=3, min_ready=rng.randint(0, 3)),
            ]
            state = _planned_state(groups)
            names = [(f"b1-{i}", "pool-a" if i < 4 else "pool-b") for i in range(7)]
            quorum_first = rng.random() < 0.5
            consumers_claimed = False
            delivered: list[str] = []
            for _ in range(60):
                action = rng.random()
                name, pool = rng.choice(names)
                if not consumers_claimed and action < 0.1:
                    if quorum_first:
                        state.claim_groups_consumer()
                    state.claim_events_consumer()
                    consumers_claimed = True
                elif action < 0.55:
                    state.upsert_claim(_ready(name, warmpool=pool))
                elif action < 0.65:
                    state.upsert_claim(_claim(name, warmpool=pool))
                elif action < 0.72:
                    state.mark_lost(name)
                elif action < 0.75:
                    state.check_deadline(rng.choice([0.0, 600.0]))
                if consumers_claimed:
                    events, _ = state.collect_events()
                    delivered += [
                        e.member.claim_name for e in events if e.type is BatchEventType.MEMBER_READY
                    ]
                    if quorum_first:
                        verdicts, _ = state.pop_group_verdicts()
                        for verdict in verdicts:
                            delivered += [m.claim_name for m in verdict.members]
            with self.subTest(seed=seed):
                self.assertEqual(len(delivered), len(set(delivered)), delivered)
