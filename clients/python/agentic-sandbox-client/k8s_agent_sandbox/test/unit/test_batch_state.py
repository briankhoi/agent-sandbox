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
"""Unit tests for the I/O-free batch state core."""

import unittest
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from k8s_agent_sandbox import batch_state
from k8s_agent_sandbox.exceptions import BatchError
from k8s_agent_sandbox.models import BatchGroup


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
    """Table test covering every Member-derivation case in the plan."""

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
        self.assertEqual(member.pod_ips, ["10.0.0.1"])
        self.assertEqual(member.service_fqdn, "sbx-1.svc")

    def test_ready_without_sandbox_name_is_not_ready(self):
        """Ready=True with no sandbox name yet cannot be a ready member."""
        member = batch_state.derive_member(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}])
        )
        self.assertFalse(member.ready)

    def test_terminal_claim_ready_reasons(self):
        from k8s_agent_sandbox.constants import TERMINAL_CLAIM_READY_REASONS

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

    def test_template_not_found_is_terminal(self):
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[
                    {"type": "Ready", "status": "False", "reason": "TemplateNotFound", "message": "x"}
                ],
            )
        )
        self.assertTrue(member.terminal)
        self.assertEqual(member.reason, "TemplateNotFound")

    def test_warmpool_not_found_is_terminal(self):
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[
                    {"type": "Ready", "status": "False", "reason": "WarmPoolNotFound", "message": "no pool"}
                ],
            )
        )
        self.assertTrue(member.terminal)
        self.assertEqual(member.reason, "WarmPoolNotFound")
        self.assertEqual(member.message, "no pool")

    def test_legacy_name_key(self):
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True"}],
                sandbox={"Name": "legacy-sandbox"},
            )
        )
        self.assertEqual(member.sandbox_name, "legacy-sandbox")
        self.assertTrue(member.ready)

    def test_pod_ips_and_service_fqdn_mapping(self):
        member = batch_state.derive_member(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True"}],
                sandbox={"name": "sbx-1", "podIPs": ["10.0.0.1", "10.0.0.2"], "serviceFQDN": "sbx-1.default.svc"},
            )
        )
        self.assertEqual(member.pod_ips, ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(member.service_fqdn, "sbx-1.default.svc")


class TestLostVsReleased(unittest.TestCase):

    def test_deleted_unreleased_claim_sets_lost(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.apply_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        lost = state.mark_deleted("b1-0")
        self.assertIsNotNone(lost)
        self.assertTrue(lost.lost)
        self.assertTrue(state.get("b1-0").lost)

    def test_deleted_released_claim_simply_disappears(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.apply_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        state.mark_released("b1-0")
        result = state.mark_deleted("b1-0")
        self.assertIsNone(result)
        self.assertIsNone(state.get("b1-0"))
        self.assertNotIn("b1-0", [m.claim_name for m in state.members()])


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

    def test_ordinal_counter_handles_gaps_and_terminating_claims(self):
        items = [
            _claim("b1-0"),
            _claim("b1-5"),
            _claim("b1-3", deletion_timestamp="2026-01-01T00:00:00Z"),
        ]
        self.assertEqual(batch_state.compute_next_ordinal("b1", items), 6)

    def test_initial_fill_is_exactly_ordinal_less_than_n(self):
        groups = [BatchGroup(warmpool="pool-a", size=2)]
        state = batch_state.BatchState("b1", groups)
        items = [_claim("b1-0"), _claim("b1-1"), _claim("b1-2")]
        state.seed_from_claims(items)
        self.assertIn("b1-0", state._initial_fill)
        self.assertIn("b1-1", state._initial_fill)
        self.assertNotIn("b1-2", state._initial_fill)


class TestSnapshots(unittest.TestCase):

    def test_returned_member_does_not_change_after_later_watch_event(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.apply_claim(_claim("b1-0", conditions=[]))
        first = state.get("b1-0")
        state.apply_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.assertFalse(first.ready)
        second = state.get("b1-0")
        self.assertTrue(second.ready)

    def test_appending_to_returned_pod_ips_does_not_affect_next_snapshot(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.apply_claim(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True"}],
                sandbox={"name": "sbx-1", "podIPs": ["10.0.0.1"]},
            )
        )
        first = state.get("b1-0")
        first.pod_ips.append("10.0.0.99")  # Mutates only the caller's own copy.

        state.apply_claim(
            _claim(
                "b1-0",
                conditions=[{"type": "Ready", "status": "True"}],
                sandbox={"name": "sbx-1", "podIPs": ["10.0.0.1"]},
            )
        )
        second = state.get("b1-0")
        self.assertEqual(second.pod_ips, ["10.0.0.1"])
        self.assertIsNot(first.pod_ips, second.pod_ips)

    def test_member_table_and_dispatched_set_keyed_by_claim_name(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        state.apply_claim(_claim("b1-0"))
        self.assertTrue(state.try_dispatch("b1-0"))
        self.assertFalse(state.try_dispatch("b1-0"))
        self.assertTrue(state.try_dispatch("b1-1"))


class TestDispatchedSet(unittest.TestCase):

    def test_try_dispatch_is_at_most_once(self):
        state = batch_state.BatchState("b1", [BatchGroup(warmpool="pool-a", size=1)])
        self.assertTrue(state.try_dispatch("b1-0"))
        self.assertFalse(state.try_dispatch("b1-0"))


class TestErrorSticky(unittest.TestCase):

    def test_first_error_wins(self):
        state = batch_state.BatchState("b1", [])
        state.note_error(ValueError("first"))
        state.note_error(ValueError("second"))
        self.assertEqual(str(state.error()), "first")


class TestBatchIdValidation(unittest.TestCase):

    def test_valid_ids_accepted(self):
        for batch_id in ("b1234567890", "a", "abc-123"):
            batch_state.validate_batch_id(batch_id)

    def test_rejects_non_letter_start(self):
        with self.assertRaises(ValueError):
            batch_state.validate_batch_id("1abc")

    def test_rejects_too_long(self):
        with self.assertRaises(ValueError):
            batch_state.validate_batch_id("a" * (batch_state.BATCH_ID_MAX_LENGTH + 1))

    def test_rejects_uppercase(self):
        with self.assertRaises(ValueError):
            batch_state.validate_batch_id("Abc")

    def test_max_length_accepted(self):
        batch_state.validate_batch_id("a" * batch_state.BATCH_ID_MAX_LENGTH)


class TestLeaseDurationValidation(unittest.TestCase):

    def test_valid_int_accepted(self):
        self.assertEqual(batch_state.validate_lease_duration_value(90), 90)

    def test_boundary_equal_to_skew_margin_raises_exact_message(self):
        with self.assertRaises(ValueError) as ctx:
            batch_state.validate_lease_duration_value(5)
        self.assertEqual(
            str(ctx.exception), "Duration must be greater than clock skew margin (5s)"
        )

    def test_invalid_values_raise(self):
        for bad in (1.5, 30.0, True, "30", 0, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    batch_state.validate_lease_duration_value(bad)


class TestLeaseDurationAnnotation(unittest.TestCase):

    def test_missing_annotation_defaults_to_60(self):
        self.assertEqual(batch_state.parse_lease_duration_annotation(None), 60)

    def test_valid_annotation_parsed(self):
        self.assertEqual(batch_state.parse_lease_duration_annotation("90"), 90)

    def test_boundary_annotation_of_6_accepted(self):
        self.assertEqual(batch_state.parse_lease_duration_annotation("6"), 6)

    def test_invalid_annotations_raise_batch_error(self):
        for bad in ("abc", "1.5", "0", "-5", "5"):
            with self.subTest(bad=bad):
                with self.assertRaises(BatchError):
                    batch_state.parse_lease_duration_annotation(bad)


class TestLeaseStaleness(unittest.TestCase):

    def test_boundary_exactly_at_skew_margin_is_not_stale(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        renew_time = now - timedelta(seconds=55)  # 60 - 5 == 55
        self.assertFalse(batch_state.is_lease_stale(renew_time, 60, now))

    def test_one_second_later_is_stale(self):
        now = datetime(2026, 1, 1, tzinfo=UTC)
        renew_time = now - timedelta(seconds=56)
        self.assertTrue(batch_state.is_lease_stale(renew_time, 60, now))


if __name__ == "__main__":
    unittest.main()
