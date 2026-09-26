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

import unittest

from pydantic import ValidationError

from k8s_agent_sandbox import batch_state
from k8s_agent_sandbox.exceptions import BatchError, QuorumUnreachableError
from k8s_agent_sandbox.models import BatchEventType, BatchGroup
from k8s_agent_sandbox.constants import (
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


class TestLost(unittest.TestCase):

    def test_deleted_claim_sets_lost(self):
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

    def test_invalid_annotation_values_raise_batch_error(self):
        for size, min_ready in (("2", "5"), ("x", "1"), ("-1", "0")):
            with self.subTest(size=size, min_ready=min_ready):
                items = [
                    _claim(
                        "b1-0",
                        annotations={
                            "agents.x-k8s.io/batch-group-size": size,
                            "agents.x-k8s.io/batch-group-min-ready": min_ready,
                        },
                    ),
                ]
                with self.assertRaises(BatchError):
                    batch_state.reconstruct_groups(items)


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


class TestErrorSticky(unittest.TestCase):

    def test_first_error_wins(self):
        state = batch_state.BatchState("b1", [])
        state.note_error(ValueError("first"))
        state.note_error(ValueError("second"))
        self.assertEqual(str(state.error()), "first")


_READY = [{"type": "Ready", "status": "True"}]
_NOT_READY = [{"type": "Ready", "status": "False", "reason": "SandboxNotReady"}]
_TERMINAL = [{"type": "Ready", "status": "False", "reason": sorted(TERMINAL_CLAIM_READY_REASONS)[0]}]


def _ready(name, warmpool="pool-a"):
    return _claim(name, warmpool, conditions=_READY, sandbox={"name": f"sbx-{name}"})


def _pending(name, warmpool="pool-a"):
    return _claim(name, warmpool, conditions=_NOT_READY)


def _state(*groups, mode=None):
    state = batch_state.BatchState("b1", list(groups))
    if mode is not None:
        state.set_mode(mode)
    return state


def _drain_events(state):
    events = []
    while (event := state.pop_event()) is not None:
        events.append((event.type, event.member.claim_name if event.member else None))
    return events


def _drain_groups(state):
    results = []
    while (result := state.pop_group_result()) is not None:
        results.append(result)
    return results


class TestStreamMode(unittest.TestCase):

    def test_each_fill_member_is_ready_once_and_non_fill_claims_emit_nothing(self):
        state = _state(BatchGroup(warmpool="pool-a", size=2), mode=batch_state.STREAM_MODE)
        state.upsert_claim(_ready("b1-0"))
        state.upsert_claim(_pending("b1-0"))
        state.upsert_claim(_ready("b1-0"))
        state.upsert_claim(_ready("b1-2"))
        state.upsert_claim(_ready("b1-1", warmpool="pool-z"))
        self.assertEqual(_drain_events(state), [(BatchEventType.MEMBER_READY, "b1-0")])

    def test_status_events(self):
        failed, lost = BatchEventType.MEMBER_FAILED, BatchEventType.MEMBER_LOST
        cases = {
            "terminal": ([lambda s: s.upsert_claim(_claim("b1-0", conditions=_TERMINAL))], [(failed, "b1-0")]),
            "lost": ([lambda s: s.upsert_claim(_pending("b1-0")), lambda s: s.mark_lost("b1-0")], [(lost, "b1-0")]),
            "create failed": ([lambda s: s.record_create_failure("b1-0", "pool-a", "forbidden")], [(failed, "b1-0")]),
            "terminal then deleted": ([
                lambda s: s.upsert_claim(_claim("b1-0", conditions=_TERMINAL)),
                lambda s: s.mark_lost("b1-0"),
            ], [(failed, "b1-0")]),
        }
        for name, (steps, want) in cases.items():
            with self.subTest(name):
                state = _state(BatchGroup(warmpool="pool-a", size=1), mode=batch_state.STREAM_MODE)
                for step in steps:
                    step(state)
                self.assertEqual(_drain_events(state), want)
        state = _state(BatchGroup(warmpool="pool-a", size=1))
        state.record_create_failure("b1-0", "pool-a", "forbidden")
        member = state.get_member("b1-0")
        self.assertEqual((member.terminal, member.reason, member.message), (True, "CreateFailed", "forbidden"))

    def test_ready_members_seen_before_a_mode_are_delivered_when_stream_mode_is_set(self):
        state = _state(BatchGroup(warmpool="pool-a", size=2))
        state.upsert_claim(_ready("b1-1"))
        state.upsert_claim(_ready("b1-0"))
        self.assertEqual(_drain_events(state), [])
        state.set_mode(batch_state.STREAM_MODE)
        self.assertEqual(
            _drain_events(state),
            [(BatchEventType.MEMBER_READY, "b1-1"), (BatchEventType.MEMBER_READY, "b1-0")],
        )


class TestQuorumMode(unittest.TestCase):

    def test_success_yields_first_min_ready_by_ready_order_then_streams_the_rest_once(self):
        state = _state(BatchGroup(warmpool="pool-a", size=4, min_ready=2))
        for name in ("b1-3", "b1-0", "b1-1"):
            state.upsert_claim(_ready(name))
        state.set_mode(batch_state.QUORUM_MODE)
        [result] = _drain_groups(state)
        self.assertIsNone(result.error)
        self.assertEqual([m.claim_name for m in result.members], ["b1-3", "b1-0"])
        self.assertEqual(_drain_events(state), [(BatchEventType.MEMBER_READY, "b1-1")])

        state.upsert_claim(_ready("b1-2"))
        self.assertEqual(_drain_events(state), [(BatchEventType.MEMBER_READY, "b1-2")])

        # A cohort member flapping, and events() joining later, hand out nothing again.
        state.upsert_claim(_pending("b1-3"))
        state.upsert_claim(_ready("b1-3"))
        state.set_mode(batch_state.STREAM_MODE)
        self.assertEqual(_drain_events(state), [])
        self.assertEqual(_drain_groups(state), [])

    def test_nothing_is_handed_out_before_the_group_yields(self):
        state = _state(BatchGroup(warmpool="pool-a", size=3), mode=batch_state.QUORUM_MODE)
        state.upsert_claim(_ready("b1-0"))
        state.upsert_claim(_ready("b1-1"))
        self.assertEqual(_drain_events(state), [])
        self.assertEqual(_drain_groups(state), [])

    def test_unreachable_group_is_finished_and_never_hands_out_members(self):
        state = _state(BatchGroup(warmpool="pool-a", size=3, min_ready=2), mode=batch_state.QUORUM_MODE)
        # b1-2 is past the first min_ready ordinals, and is still held while the group has no verdict.
        state.upsert_claim(_ready("b1-2"))
        state.upsert_claim(_claim("b1-0", conditions=_TERMINAL))
        self.assertEqual(_drain_groups(state), [])
        state.upsert_claim(_pending("b1-1"))
        state.mark_lost("b1-1")

        [result] = _drain_groups(state)
        self.assertIsInstance(result.error, QuorumUnreachableError)
        self.assertEqual(
            (result.error.size, result.error.min_ready, result.error.failed, result.error.lost),
            (3, 2, 1, 1),
        )
        self.assertEqual(result.members, [])
        self.assertTrue(state.group_failed("pool-a"))

        state.upsert_claim(_pending("b1-2"))
        state.upsert_claim(_ready("b1-2"))
        self.assertEqual(
            _drain_events(state),
            [(BatchEventType.MEMBER_FAILED, "b1-0"), (BatchEventType.MEMBER_LOST, "b1-1")],
        )
        self.assertEqual([m.claim_name for m in state.members()], ["b1-0", "b1-1", "b1-2"])

    def test_expire_fill_times_out_undecided_groups_only(self):
        state = _state(
            BatchGroup(warmpool="pool-a", size=1),
            BatchGroup(warmpool="pool-b", size=1),
            mode=batch_state.QUORUM_MODE,
        )
        state.upsert_claim(_ready("b1-0", warmpool="pool-a"))
        state.upsert_claim(_pending("b1-1", warmpool="pool-b"))
        self.assertFalse(state.fill_settled())
        state.expire_fill()

        results = {r.warmpool: r for r in _drain_groups(state)}
        self.assertIsNone(results["pool-a"].error)
        self.assertIsInstance(results["pool-b"].error, TimeoutError)
        self.assertTrue(state.fill_settled())
        self.assertTrue(state.groups_done())

    def test_mode_rules(self):
        state = _state(BatchGroup(warmpool="pool-a", size=1), mode=batch_state.STREAM_MODE)
        with self.assertRaises(BatchError):
            state.set_mode(batch_state.QUORUM_MODE)

        state = _state(BatchGroup(warmpool="pool-a", size=1), mode=batch_state.QUORUM_MODE)
        state.set_mode(batch_state.STREAM_MODE)

        state = _state(BatchGroup(warmpool="pool-a", size=2, min_ready=0), mode=batch_state.QUORUM_MODE)
        [result] = _drain_groups(state)
        self.assertEqual((result.members, result.error), ([], None))


class TestSettle(unittest.TestCase):

    def test_events_done_exactly_when_the_last_pending_member_resolves(self):
        state = _state(BatchGroup(warmpool="pool-a", size=3), mode=batch_state.STREAM_MODE)
        state.upsert_claim(_ready("b1-0"))
        state.record_create_failure("b1-1", "pool-a", "forbidden")
        self.assertFalse(state.fill_settled())
        state.cancel_create("pool-a")
        self.assertTrue(state.fill_settled())
        self.assertFalse(state.events_done())
        _drain_events(state)
        self.assertTrue(state.events_done())


class TestReattachedFill(unittest.TestCase):

    def test_seeding_hands_out_lower_ordinals_first(self):
        state = _state(BatchGroup(warmpool="pool-a", size=11, min_ready=1))
        state.seed_from_claims([_ready("b1-10"), _ready("b1-2")])
        state.set_mode(batch_state.STREAM_MODE)
        self.assertEqual(
            _drain_events(state),
            [(BatchEventType.MEMBER_READY, "b1-2"), (BatchEventType.MEMBER_READY, "b1-10")],
        )

    def test_missing_fill_claims_count_as_unable(self):
        state = _state(BatchGroup(warmpool="pool-a", size=3, min_ready=2))
        state.seed_from_claims([_ready("b1-0")])
        state.count_missing_fill_as_unable()
        self.assertTrue(state.fill_settled())
        state.set_mode(batch_state.QUORUM_MODE)
        [result] = _drain_groups(state)
        self.assertIsInstance(result.error, QuorumUnreachableError)


if __name__ == "__main__":
    unittest.main()
