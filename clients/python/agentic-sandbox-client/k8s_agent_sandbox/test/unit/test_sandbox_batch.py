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
"""Unit tests for the sync SandboxBatch handle and SandboxClient.get_batch."""

import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, call, patch

import urllib3.exceptions
from kubernetes import client as k8s_client

from k8s_agent_sandbox import batch_state
from k8s_agent_sandbox.batch_state import (
    BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS,
    BATCH_RELEASE_MAX_DELETE_ROUNDS,
    CLOCK_SKEW_MARGIN,
)
from k8s_agent_sandbox.constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    BATCH_ID_LABEL,
    BATCH_LEASE_DURATION_ANNOTATION,
    BATCH_QUORUM_TIMEOUT_ANNOTATION,
    BATCH_WORK_BUDGET_ANNOTATION,
    CREATED_BY_LABEL,
)
from k8s_agent_sandbox.exceptions import (
    BatchError,
    BatchExistsError,
    BatchInUseError,
    BatchLeaseExpiredError,
    BatchNotFoundError,
    QuorumUnreachableError,
    SandboxNotReadyError,
    SandboxTemplateNotFoundError,
    SandboxWarmPoolNotFoundError,
)
from k8s_agent_sandbox.models import BatchEventType, BatchGroup, Member
from k8s_agent_sandbox.sandbox_batch import SandboxBatch
from k8s_agent_sandbox.sandbox_client import SandboxClient


def _lease(
    holder_identity=None,
    renew_time=None,
    lease_duration_seconds=BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    annotations=None,
    resource_version="10",
):
    return k8s_client.V1Lease(
        metadata=k8s_client.V1ObjectMeta(
            name="batch-b1", resource_version=resource_version, annotations=annotations or {}
        ),
        spec=k8s_client.V1LeaseSpec(
            holder_identity=holder_identity,
            renew_time=renew_time,
            lease_duration_seconds=lease_duration_seconds,
        ),
    )


def _claim(name, warmpool="pool-a", conditions=None, sandbox=None, annotations=None):
    metadata: dict = {"name": name}
    if annotations:
        metadata["annotations"] = annotations
    status = {}
    if conditions is not None:
        status["conditions"] = conditions
    if sandbox is not None:
        status["sandbox"] = sandbox
    return {"metadata": metadata, "spec": {"warmPoolRef": {"name": warmpool}}, "status": status}


class BaseBatchClientTest(unittest.TestCase):
    @patch("k8s_agent_sandbox.sandbox_client.K8sHelper")
    def setUp(self, MockK8sHelper):
        self.client = SandboxClient()
        self.mock_k8s_helper = self.client.k8s_helper
        self.mock_sandbox_class = MagicMock()
        self.client.sandbox_class = self.mock_sandbox_class


@patch.object(SandboxBatch, "_start", lambda self: None)
class TestGetBatchLeaseCases(BaseBatchClientTest):

    def test_not_found_raises(self):
        self.mock_k8s_helper.read_batch_lease.return_value = None
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([], "0")
        with self.assertRaises(BatchNotFoundError):
            self.client.get_batch("b1")

    def test_stale_held_lease_raises_expired(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity="someone-else", renew_time=now - timedelta(seconds=120)
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        with self.assertRaises(BatchLeaseExpiredError):
            self.client.get_batch("b1")

    def test_missing_lease_with_claims_present_raises_expired_and_creates_nothing(self):
        self.mock_k8s_helper.read_batch_lease.return_value = None
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        with self.assertRaises(BatchLeaseExpiredError):
            self.client.get_batch("b1")
        self.mock_k8s_helper.replace_batch_lease.assert_not_called()

    def test_unheld_but_stale_lease_raises_expired(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now - timedelta(seconds=120)
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        with self.assertRaises(BatchLeaseExpiredError):
            self.client.get_batch("b1")

    def test_skew_margin_boundary_adoptable_then_expired_one_second_later(self):
        now = datetime.now(UTC)

        with patch("k8s_agent_sandbox.sandbox_batch.datetime") as mock_dt:
            mock_dt.now.return_value = now
            self.mock_k8s_helper.read_batch_lease.return_value = _lease(
                holder_identity=None,
                renew_time=now - timedelta(seconds=BATCH_DEFAULT_LEASE_DURATION_SECONDS - CLOCK_SKEW_MARGIN),
            )
            self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
            batch = self.client.get_batch("b1")
            self.assertIsNotNone(batch)

        with patch("k8s_agent_sandbox.sandbox_batch.datetime") as mock_dt2:
            mock_dt2.now.return_value = now
            self.mock_k8s_helper.read_batch_lease.return_value = _lease(
                holder_identity=None,
                renew_time=now - timedelta(seconds=BATCH_DEFAULT_LEASE_DURATION_SECONDS - CLOCK_SKEW_MARGIN + 1),
            )
            with self.assertRaises(BatchLeaseExpiredError):
                self.client.get_batch("b1")

    def test_live_different_holder_raises_in_use(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity="other-holder", renew_time=now
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        with self.assertRaises(BatchInUseError):
            self.client.get_batch("b1")

    def test_live_unheld_takes_over(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = (
            [_claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})],
            "5",
        )
        batch = self.client.get_batch("b1")
        self.assertEqual(batch.batch_id, "b1")
        self.mock_k8s_helper.replace_batch_lease.assert_called_once()

    def test_takeover_write_conflict_raises_in_use(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        conflict = k8s_client.ApiException(status=409)
        self.mock_k8s_helper.replace_batch_lease.side_effect = conflict
        with self.assertRaises(BatchInUseError) as ctx:
            self.client.get_batch("b1")
        self.assertIs(ctx.exception.__cause__, conflict)

    def test_takeover_write_other_error_propagates(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        self.mock_k8s_helper.replace_batch_lease.side_effect = k8s_client.ApiException(status=500)
        with self.assertRaises(k8s_client.ApiException):
            self.client.get_batch("b1")


@patch.object(SandboxBatch, "_start", lambda self: None)
class TestBatchStaleness(BaseBatchClientTest):

    def test_staleness_follows_spec_duration_not_annotation_duration(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None,
            renew_time=now - timedelta(seconds=30),
            lease_duration_seconds=10,
            annotations={BATCH_LEASE_DURATION_ANNOTATION: str(BATCH_DEFAULT_LEASE_DURATION_SECONDS)},
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        with self.assertRaises(BatchLeaseExpiredError):
            self.client.get_batch("b1")
        self.mock_k8s_helper.replace_batch_lease.assert_not_called()

    def test_spec_duration_boundary_adoptable_then_expired_one_second_later(self):
        now = datetime.now(UTC)
        spec_duration = 30
        with patch("k8s_agent_sandbox.sandbox_batch.datetime") as mock_dt:
            mock_dt.now.return_value = now
            self.mock_k8s_helper.read_batch_lease.return_value = _lease(
                holder_identity=None,
                renew_time=now - timedelta(seconds=spec_duration - CLOCK_SKEW_MARGIN),
                lease_duration_seconds=spec_duration,
                annotations={BATCH_LEASE_DURATION_ANNOTATION: "90"},
            )
            self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
            batch = self.client.get_batch("b1")
            self.assertIsNotNone(batch)

        with patch("k8s_agent_sandbox.sandbox_batch.datetime") as mock_dt2:
            mock_dt2.now.return_value = now
            self.mock_k8s_helper.read_batch_lease.return_value = _lease(
                holder_identity=None,
                renew_time=now - timedelta(seconds=spec_duration - CLOCK_SKEW_MARGIN + 1),
                lease_duration_seconds=spec_duration,
                annotations={BATCH_LEASE_DURATION_ANNOTATION: "90"},
            )
            with self.assertRaises(BatchLeaseExpiredError):
                self.client.get_batch("b1")

    def test_missing_spec_duration_is_treated_as_stale(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now, lease_duration_seconds=None
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        with self.assertRaises(BatchLeaseExpiredError):
            self.client.get_batch("b1")
        self.mock_k8s_helper.replace_batch_lease.assert_not_called()

    def test_missing_renew_time_is_treated_as_stale(self):
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None,
            renew_time=None,
            lease_duration_seconds=BATCH_DEFAULT_LEASE_DURATION_SECONDS,
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        with self.assertRaises(BatchLeaseExpiredError):
            self.client.get_batch("b1")
        self.mock_k8s_helper.replace_batch_lease.assert_not_called()


@patch.object(SandboxBatch, "_start", lambda self: None)
class TestLeaseTakeoverWrites(BaseBatchClientTest):

    def test_writes_holder_identity_and_renew_time_near_now(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertIsNotNone(written_lease.spec.holder_identity)
        self.assertLess(
            abs((written_lease.spec.renew_time - datetime.now(UTC)).total_seconds()), 5
        )

    def test_annotation_duration_overrides_leftover_spec_duration_on_takeover(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None,
            renew_time=now,
            lease_duration_seconds=300,  # leftover detach grace
            annotations={BATCH_LEASE_DURATION_ANNOTATION: "90"},
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        batch = self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written_lease.spec.lease_duration_seconds, 90)
        self.assertEqual(batch._lease_duration, 90)

    def test_no_annotation_takeover_writes_default_duration(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        batch = self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written_lease.spec.lease_duration_seconds, BATCH_DEFAULT_LEASE_DURATION_SECONDS)
        self.assertEqual(batch._lease_duration, BATCH_DEFAULT_LEASE_DURATION_SECONDS)

    def test_invalid_annotations_raise_batch_error_with_no_write(self):
        now = datetime.now(UTC)
        for bad in ("abc", "1.5", "0", "-5", "5"):
            with self.subTest(bad=bad):
                self.mock_k8s_helper.reset_mock()
                self.mock_k8s_helper.read_batch_lease.return_value = _lease(
                    holder_identity=None,
                    renew_time=now,
                    annotations={BATCH_LEASE_DURATION_ANNOTATION: bad},
                )
                self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
                with self.assertRaises(BatchError):
                    self.client.get_batch("b1")
                self.mock_k8s_helper.replace_batch_lease.assert_not_called()

    def test_annotation_of_skew_margin_plus_one_succeeds(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None,
            renew_time=now,
            annotations={BATCH_LEASE_DURATION_ANNOTATION: str(CLOCK_SKEW_MARGIN + 1)},
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        batch = self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written_lease.spec.lease_duration_seconds, CLOCK_SKEW_MARGIN + 1)

    def test_conflicting_group_annotations_raise_batch_error_with_no_write(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=now
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = (
            [
                _claim(
                    "b1-0",
                    annotations={
                        BATCH_GROUP_SIZE_ANNOTATION: "3",
                        BATCH_GROUP_MIN_READY_ANNOTATION: "2",
                    },
                ),
                _claim(
                    "b1-1",
                    annotations={
                        BATCH_GROUP_SIZE_ANNOTATION: "5",
                        BATCH_GROUP_MIN_READY_ANNOTATION: "2",
                    },
                ),
            ],
            "5",
        )
        with self.assertRaises(BatchError):
            self.client.get_batch("b1")
        self.mock_k8s_helper.replace_batch_lease.assert_not_called()

    def test_takeover_leaves_annotation_unchanged(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None,
            renew_time=now,
            annotations={BATCH_LEASE_DURATION_ANNOTATION: "90"},
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(
            written_lease.metadata.annotations, {BATCH_LEASE_DURATION_ANNOTATION: "90"}
        )


def _make_handle(
    client,
    batch_id="b1",
    namespace="default",
    groups=None,
    lease_duration=BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    **kwargs,
):
    groups = groups if groups is not None else [BatchGroup(warmpool="pool-a", size=1)]
    state = batch_state.BatchState(batch_id, groups)
    return SandboxBatch(
        client=client,
        batch_id=batch_id,
        namespace=namespace,
        state=state,
        lease_name=f"batch-{batch_id}",
        holder_identity="host_1234_abcd1234",
        lease_duration=lease_duration,
        list_resource_version="5",
        **kwargs,
    )


class TestWatcher(BaseBatchClientTest):

    def _terminating_error(self, status=403):
        return k8s_client.ApiException(status=status)

    def test_resumes_from_list_resource_version(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [self._terminating_error()]
        handle._watch_loop()
        first_call = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[0]
        self.assertEqual(first_call.args[2], "5")

    def test_410_relists_and_diffs_vanished_claim_becomes_lost(self):
        handle = _make_handle(self.client)
        handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [
            self._terminating_error(status=410),
            self._terminating_error(status=403),
        ]
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([], "9")
        handle._watch_loop()
        self.assertTrue(handle._state.get_member("b1-0").lost)

    def test_410_relist_retries_503_then_succeeds_reconciling_lost_claim(self):
        handle = _make_handle(self.client)
        handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        calls = {"n": 0}

        def watch_side_effect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._terminating_error(status=410)
            handle._watch_stop.set()
            return iter(())

        self.mock_k8s_helper.watch_sandbox_claims.side_effect = watch_side_effect
        self.mock_k8s_helper.list_sandbox_claim_objects.side_effect = [
            self._terminating_error(status=503),
            ([], "9"),
        ]
        handle._watch_loop()
        self.assertTrue(handle._state.get_member("b1-0").lost)
        self.assertIsNone(handle.err())
        list_calls = self.mock_k8s_helper.list_sandbox_claim_objects.call_args_list
        self.assertEqual(len(list_calls), 2)

    def test_410_relist_403_surfaces_through_err_and_stops(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [self._terminating_error(status=410)]
        self.mock_k8s_helper.list_sandbox_claim_objects.side_effect = [
            self._terminating_error(status=403)
        ]
        handle._watch_loop()
        self.assertIsInstance(handle.err(), k8s_client.ApiException)
        self.assertEqual(handle.err().status, 403)

    def test_410_relist_after_stop_set_does_not_touch_state(self):
        handle = _make_handle(self.client)
        handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [self._terminating_error(status=410)]

        def list_side_effect(*args, **kwargs):
            handle._watch_stop.set()
            return ([], "9")

        self.mock_k8s_helper.list_sandbox_claim_objects.side_effect = list_side_effect
        handle._watch_loop()
        self.assertFalse(handle._state.get_member("b1-0").lost)

    def test_disconnect_reconnects_from_last_resource_version(self):
        handle = _make_handle(self.client)

        def first_stream(*args, **kwargs):
            yield {"type": "MODIFIED", "object": {"metadata": {"name": "b1-0", "resourceVersion": "77"}, "spec": {"warmPoolRef": {"name": "pool-a"}}, "status": {}}}
            raise urllib3.exceptions.ProtocolError("boom")

        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [
            first_stream(),
            self._terminating_error(status=403),
        ]
        handle._watch_loop()
        second_call = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[1]
        self.assertEqual(second_call.args[2], "77")

    def test_bookmark_only_advances_resource_version(self):
        handle = _make_handle(self.client)
        bookmark_event = {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "42"}}}
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [
            [bookmark_event],
            self._terminating_error(status=403),
        ]
        handle._watch_loop()
        self.assertEqual(len(handle._state.members()), 0)
        second_call = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[1]
        self.assertEqual(second_call.args[2], "42")

    def test_403_surfaces_through_err(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [self._terminating_error(status=403)]
        handle._watch_loop()
        self.assertIsInstance(handle.err(), k8s_client.ApiException)
        self.assertEqual(handle.err().status, 403)

    def test_error_after_stop_set_leaves_err_none(self):
        handle = _make_handle(self.client)

        def watch_side_effect(*args, **kwargs):
            handle._watch_stop.set()
            raise self._terminating_error(status=403)

        self.mock_k8s_helper.watch_sandbox_claims.side_effect = watch_side_effect
        handle._watch_loop()
        self.assertIsNone(handle.err())

    def _retry_then_stop(self, handle, status):
        """A watch attempt raising ``status`` followed by one that succeeds
        with no events, at which point the test stops the loop from inside
        the mocked call itself (there is no third attempt, so ``err()``
        never gets a chance to be set by anything after the retry)."""
        calls = {"n": 0}

        def side_effect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise self._terminating_error(status=status)
            handle._watch_stop.set()
            return iter(())

        self.mock_k8s_helper.watch_sandbox_claims.side_effect = side_effect

    def test_503_retries_from_last_resource_version_and_leaves_err_none(self):
        handle = _make_handle(self.client)
        self._retry_then_stop(handle, 503)
        handle._watch_loop()
        self.assertIsNone(handle.err())
        calls = self.mock_k8s_helper.watch_sandbox_claims.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].args[2], "5")

    def test_429_retries_from_last_resource_version_and_leaves_err_none(self):
        handle = _make_handle(self.client)
        self._retry_then_stop(handle, 429)
        handle._watch_loop()
        self.assertIsNone(handle.err())
        calls = self.mock_k8s_helper.watch_sandbox_claims.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].args[2], "5")

    def test_ssl_error_surfaces_through_err_and_stops(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [
            urllib3.exceptions.SSLError("boom")
        ]
        handle._watch_loop()
        self.assertIsInstance(handle.err(), urllib3.exceptions.SSLError)

    def test_unexpected_error_surfaces_through_err_and_stops(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [RuntimeError("boom")]
        handle._watch_loop()
        self.assertIsInstance(handle.err(), RuntimeError)

    def test_label_selector_is_exact(self):
        handle = _make_handle(self.client, batch_id="b1234")
        self.mock_k8s_helper.watch_sandbox_claims.side_effect = [self._terminating_error(status=403)]
        handle._watch_loop()
        call_args = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[0]
        self.assertEqual(call_args.args[1], f"{BATCH_ID_LABEL}=b1234")


class TestRenewal(BaseBatchClientTest):

    def test_renewal_uses_read_resource_version(self):
        handle = _make_handle(self.client)
        lease = _lease(holder_identity=handle._holder_identity, resource_version="123")
        self.mock_k8s_helper.read_batch_lease.return_value = lease
        self.assertTrue(handle._renew_once())
        self.mock_k8s_helper.replace_batch_lease.assert_called_once_with(
            handle._lease_name, handle.namespace, lease
        )
        self.assertEqual(lease.metadata.resource_version, "123")

    def test_first_failure_logs_degraded(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.read_batch_lease.side_effect = RuntimeError("api down")
        with self.assertLogs(level="INFO") as ctx:
            handle._renew_once()
        self.assertTrue(any("degraded" in msg for msg in ctx.output))
        self.assertTrue(handle._renewal_degraded)

    def test_holder_mismatch_stops_renewal_without_writing(self):
        for other_holder in ("someone-else", None):
            with self.subTest(other_holder=other_holder):
                self.mock_k8s_helper.reset_mock()
                handle = _make_handle(self.client)
                self.mock_k8s_helper.read_batch_lease.return_value = _lease(
                    holder_identity=other_holder
                )
                self.assertFalse(handle._renew_once())
                self.mock_k8s_helper.replace_batch_lease.assert_not_called()
                self.assertIsInstance(handle.err(), BatchInUseError)

    def test_missing_lease_stops_renewal_and_records_expired(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.read_batch_lease.return_value = None
        self.assertFalse(handle._renew_once())
        self.mock_k8s_helper.replace_batch_lease.assert_not_called()
        self.assertIsInstance(handle.err(), BatchLeaseExpiredError)

    def test_successful_renewal_clears_degraded(self):
        handle = _make_handle(self.client)
        handle._renewal_degraded = True
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=handle._holder_identity
        )
        handle._renew_once()
        self.assertFalse(handle._renewal_degraded)

    def test_no_success_for_lease_duration_returns_expired_and_sticks(self):
        handle = _make_handle(self.client, lease_duration=BATCH_DEFAULT_LEASE_DURATION_SECONDS)
        handle._last_renew_success = time.monotonic() - (BATCH_DEFAULT_LEASE_DURATION_SECONDS + 1)
        self.mock_k8s_helper.read_batch_lease.side_effect = RuntimeError("api down")
        handle._renew_once()
        self.assertIsInstance(handle.err(), BatchLeaseExpiredError)


class TestMembers(unittest.TestCase):

    def test_warmpool_filter_and_snapshot_immutability(self):
        client_mock = MagicMock()
        handle = _make_handle(client_mock)
        handle._state.upsert_claim(_claim("b1-0", warmpool="pool-a"))
        handle._state.upsert_claim(_claim("b1-1", warmpool="pool-b"))

        pool_a = handle.members(warmpool="pool-a")
        self.assertEqual([m.claim_name for m in pool_a], ["b1-0"])

        first = handle.members()[0]
        handle._state.upsert_claim(
            _claim("b1-0", warmpool="pool-a", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.assertFalse(first.ready)


class TestConnect(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_sandbox_instance = MagicMock()
        self.mock_client.sandbox_class.return_value = self.mock_sandbox_instance
        self.handle = _make_handle(self.mock_client)

    def test_builds_sandbox_class_without_resolve_or_get_sandbox(self):
        self.handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = self.handle.members()[0]
        sandbox = self.handle.connect(member)

        self.mock_client.sandbox_class.assert_called_once_with(
            claim_name="b1-0",
            sandbox_id="sbx-1",
            namespace="default",
            connection_config=self.mock_client.connection_config,
            tracer_config=self.mock_client.tracer_config,
            k8s_helper=self.mock_client.k8s_helper,
        )
        self.mock_client.k8s_helper.resolve_sandbox_name.assert_not_called()
        self.mock_client.k8s_helper.get_sandbox.assert_not_called()
        self.assertIs(sandbox, self.mock_sandbox_instance)

    def test_repeat_call_returns_same_handle(self):
        self.handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = self.handle.members()[0]
        first = self.handle.connect(member)
        second = self.handle.connect(member)
        self.assertIs(first, second)
        self.mock_client.sandbox_class.assert_called_once()

    def test_not_ready_member_raises(self):
        self.handle._state.upsert_claim(_claim("b1-0", conditions=[]))
        member = self.handle.members()[0]
        with self.assertRaises(SandboxNotReadyError):
            self.handle.connect(member)

    def test_lost_member_raises(self):
        self.handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = self.handle.members()[0]
        self.handle._state.mark_lost("b1-0")
        with self.assertRaises(SandboxNotReadyError):
            self.handle.connect(member)

    @patch("k8s_agent_sandbox.sandbox_client.K8sHelper")
    def test_connected_handle_not_registered_in_active_connection_sandboxes(self, MockK8sHelper):
        real_client = SandboxClient()
        real_client.sandbox_class = MagicMock(return_value=MagicMock())
        handle = _make_handle(real_client)
        handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = handle.members()[0]
        handle.connect(member)
        self.assertEqual(real_client._active_connection_sandboxes, {})


class TestDetach(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.handle = _make_handle(self.mock_client)

    def test_invalid_grace_values_raise_value_error_leave_handle_running(self):
        for bad in (1.5, 30.0, True, "30", 0, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.handle.detach(grace=bad)
                self.mock_client.k8s_helper.replace_batch_lease.assert_not_called()
                self.assertFalse(self.handle._detached)

    def test_grace_of_skew_margin_plus_one_is_accepted(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=self.handle._holder_identity
        )
        self.handle.detach(grace=CLOCK_SKEW_MARGIN + 1)
        written = self.mock_client.k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written.spec.lease_duration_seconds, CLOCK_SKEW_MARGIN + 1)

    def test_final_write_clears_holder_sets_renew_time_and_grace_duration(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=self.handle._holder_identity,
            annotations={BATCH_LEASE_DURATION_ANNOTATION: str(BATCH_DEFAULT_LEASE_DURATION_SECONDS)},
        )
        self.handle.detach(grace=10)
        written = self.mock_client.k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertIsNone(written.spec.holder_identity)
        self.assertLess(abs((written.spec.renew_time - datetime.now(UTC)).total_seconds()), 5)
        self.assertEqual(written.spec.lease_duration_seconds, 10)
        self.assertEqual(
            written.metadata.annotations,
            {BATCH_LEASE_DURATION_ANNOTATION: str(BATCH_DEFAULT_LEASE_DURATION_SECONDS)},
        )

    def test_grace_none_uses_handles_lease_duration(self):
        handle = _make_handle(self.mock_client, lease_duration=90)
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=handle._holder_identity
        )
        handle.detach(grace=None)
        written = self.mock_client.k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written.spec.lease_duration_seconds, 90)

    def test_idempotent_detach(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=self.handle._holder_identity
        )
        mock_sandbox = MagicMock()
        self.handle._connected["b1-0"] = mock_sandbox

        self.handle.detach()

        mock_sandbox.close_connection.assert_called_once()
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")
        self.assertEqual(self.handle._connected, {})

        self.handle.detach()  # Second call: no exception, and does nothing.

        mock_sandbox.close_connection.assert_called_once()
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")
        self.mock_client.k8s_helper.read_batch_lease.assert_called_once()
        self.mock_client.k8s_helper.replace_batch_lease.assert_called_once()

    def test_retryable_after_release_write_failure(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=self.handle._holder_identity
        )
        self.mock_client.k8s_helper.replace_batch_lease.side_effect = RuntimeError("api down")

        with self.assertRaises(RuntimeError):
            self.handle.detach()
        self.assertTrue(self.handle._detached)
        self.assertFalse(self.handle._lease_released)
        self.mock_client._unregister_batch.assert_not_called()

        self.mock_client.k8s_helper.replace_batch_lease.side_effect = None
        self.handle.detach()  # Retry: completes the release.
        self.assertTrue(self.handle._lease_released)
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")

    def test_close_connection_failure_still_releases_lease(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=self.handle._holder_identity
        )
        bad_sandbox = MagicMock()
        bad_sandbox.close_connection.side_effect = RuntimeError("boom")
        self.handle._connected["b1-0"] = bad_sandbox

        with self.assertLogs(level="WARNING"):
            self.handle.detach()  # Must not raise.

        self.mock_client.k8s_helper.replace_batch_lease.assert_called_once()
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")

    def test_foreign_holder_lease_is_left_untouched(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity="someone-else"
        )

        self.handle.detach()  # Must not raise.

        self.mock_client.k8s_helper.replace_batch_lease.assert_not_called()
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")

    def test_connect_after_detach_raises(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease()
        self.handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = self.handle.members()[0]
        self.handle.detach()
        with self.assertRaises(BatchError):
            self.handle.connect(member)

    def test_members_and_err_keep_last_snapshot_after_detach(self):
        self.mock_client.k8s_helper.read_batch_lease.return_value = _lease()
        self.handle._state.upsert_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.handle._state.note_error(RuntimeError("boom"))

        self.handle.detach()

        self.assertEqual([m.claim_name for m in self.handle.members()], ["b1-0"])
        self.assertIsInstance(self.handle.err(), RuntimeError)


class TestStart(unittest.TestCase):

    def test_start_launches_two_daemon_threads(self):
        mock_client = MagicMock()
        handle = _make_handle(mock_client)
        with patch.object(SandboxBatch, "_watch_loop", lambda self: None), \
             patch.object(SandboxBatch, "_renew_loop", lambda self: None):
            handle._start()
            handle._watch_thread.join(timeout=2)
            handle._renew_thread.join(timeout=2)
        self.assertTrue(handle._watch_thread.daemon)
        self.assertTrue(handle._renew_thread.daemon)


_READY = [{"type": "Ready", "status": "True"}]


def _ready(name, warmpool="pool-a"):
    return _claim(name, warmpool=warmpool, conditions=_READY, sandbox={"name": f"sbx-{name}"})


def _terminal(name, warmpool="pool-a", reason="InvalidMetadata"):
    return _claim(
        name, warmpool=warmpool, conditions=[{"type": "Ready", "status": "False", "reason": reason}]
    )


class _FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _apply(handle, claim=None, lost=None):
    """Applies a watch update to the handle's state the way the watch loop does."""
    with handle._cond:
        if claim is not None:
            handle._state.upsert_claim(claim)
        if lost is not None:
            handle._state.mark_lost(lost)
        handle._cond.notify_all()


def _planned_handle(client, groups, clock=None, **kwargs):
    handle = _make_handle(client, groups=groups, clock=clock or _FakeClock(), **kwargs)
    plan = handle._state.plan_initial_fill()
    return handle, plan


def _api_error(status, headers=None):
    error = k8s_client.ApiException(status=status)
    error.headers = headers
    return error


def _idle_watch():
    """A watch that yields nothing and ends shortly, like a quiet server-side timeout."""
    def watch(*args, **kwargs):
        def stream():
            time.sleep(0.01)
            return
            yield

        return stream()

    return watch


def _stop(handle):
    handle._create_stop.set()
    handle._stop_background_threads()
    if handle._create_thread is not None:
        handle._create_thread.join(timeout=5)


class BaseClaimBatchTest(BaseBatchClientTest):

    def setUp(self):
        super().setUp()
        helper = self.mock_k8s_helper
        helper.get_sandbox_warmpool.return_value = {"spec": {"sandboxTemplateRef": {"name": "tmpl"}}}
        helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
        helper.list_sandbox_claim_objects.return_value = ([], "1")
        helper.watch_sandbox_claims.side_effect = _idle_watch()

    def tearDown(self):
        for batch in list(self.client._active_batches.values()):
            _stop(batch)

    def _claim_batch(self, groups=None, **kwargs):
        groups = groups if groups is not None else [BatchGroup(warmpool="pool-a", size=2)]
        kwargs.setdefault("batch_id", "b1")
        return self.client.claim_batch(groups, **kwargs)

    def _wait_for_creates(self, batch):
        batch._create_thread.join(timeout=5)
        self.assertFalse(batch._create_thread.is_alive())


class TestClaimBatchValidation(BaseClaimBatchTest):

    def _assert_rejected(self, groups=None, **kwargs):
        with self.assertRaises(ValueError) as ctx:
            self._claim_batch(groups, **kwargs)
        self.mock_k8s_helper.get_sandbox_warmpool.assert_not_called()
        self.mock_k8s_helper.create_batch_lease.assert_not_called()
        self.mock_k8s_helper.create_sandbox_claim.assert_not_called()
        return ctx.exception

    def test_group_rules(self):
        cases = {
            "empty": [],
            "size zero": [BatchGroup(warmpool="pool-a", size=0)],
            "negative size": [BatchGroup.model_construct(warmpool="pool-a", size=-1, min_ready=0)],
            "duplicate pool": [BatchGroup(warmpool="pool-a", size=1), BatchGroup(warmpool="pool-a", size=1)],
        }
        for name, groups in cases.items():
            with self.subTest(name):
                self._assert_rejected(groups)

    def test_reserved_label_and_bad_batch_id(self):
        self._assert_rejected(labels={BATCH_ID_LABEL: "other"})
        self._assert_rejected(batch_id="1-starts-with-digit")

    def test_work_budget_and_quorum_timeout_must_be_positive_ints(self):
        for field in ("work_budget", "quorum_timeout"):
            for bad in (1.5, 60.0, True, "60", 0, -5):
                with self.subTest(field=field, bad=bad):
                    self._assert_rejected(**{field: bad})

    def test_lease_duration_rules(self):
        for bad in (1.5, 60.0, True, "60", 0, -5, 5):
            with self.subTest(bad=bad):
                self._assert_rejected(lease_duration=bad)
        error = self._assert_rejected(lease_duration=5)
        self.assertEqual(str(error), "Duration must be greater than clock skew margin (5s)")
        batch = self._claim_batch(lease_duration=6)
        self.assertEqual(batch._lease_duration, 6)


class TestClaimBatchSequence(BaseClaimBatchTest):

    def test_lease_then_list_then_watch_then_first_create(self):
        calls = []
        helper = self.mock_k8s_helper
        helper.create_batch_lease.side_effect = lambda *a, **k: calls.append("lease")
        helper.list_sandbox_claim_objects.side_effect = lambda *a, **k: (calls.append("list"), ([], "1"))[1]
        helper.create_sandbox_claim.side_effect = lambda *a, **k: calls.append("create")
        original_start = SandboxBatch._start

        def start(handle):
            calls.append("watch")
            original_start(handle)

        with patch.object(SandboxBatch, "_start", start):
            batch = self._claim_batch()
            self._wait_for_creates(batch)

        firsts = [calls.index(step) for step in ("lease", "list", "watch", "create")]
        self.assertEqual(firsts, sorted(firsts))
        self.assertEqual(calls.count("create"), 2)

    def test_lease_conflict_raises_batch_exists_and_creates_nothing(self):
        self.mock_k8s_helper.create_batch_lease.side_effect = _api_error(409)
        with self.assertRaises(BatchExistsError):
            self._claim_batch()
        self.mock_k8s_helper.list_sandbox_claim_objects.assert_not_called()
        self.mock_k8s_helper.create_sandbox_claim.assert_not_called()
        self.assertEqual(self.client._active_batches, {})

    def test_list_failure_deletes_lease_and_reraises(self):
        error = _api_error(500)
        self.mock_k8s_helper.list_sandbox_claim_objects.side_effect = error
        with self.assertRaises(k8s_client.ApiException) as ctx:
            self._claim_batch()
        self.assertIs(ctx.exception, error)
        self.mock_k8s_helper.delete_batch_lease.assert_called_once_with("batch-b1", "default")
        self.mock_k8s_helper.create_sandbox_claim.assert_not_called()

    def test_watch_start_failure_deletes_lease_and_reraises(self):
        with patch.object(SandboxBatch, "_start", side_effect=RuntimeError("no threads")):
            with self.assertRaises(RuntimeError):
                self._claim_batch()
        self.mock_k8s_helper.delete_batch_lease.assert_called_once_with("batch-b1", "default")
        self.mock_k8s_helper.create_sandbox_claim.assert_not_called()
        self.assertEqual(self.client._active_batches, {})

    def test_returns_before_creates_finish(self):
        gate = threading.Event()
        self.mock_k8s_helper.create_sandbox_claim.side_effect = lambda *a, **k: gate.wait(5)
        batch = self._claim_batch(groups=[BatchGroup(warmpool="pool-a", size=3)])
        self.assertTrue(batch._create_thread.is_alive())
        self.assertEqual(self.client._active_batches, {("default", "b1"): batch})
        gate.set()
        self._wait_for_creates(batch)
        self.assertEqual(self.mock_k8s_helper.create_sandbox_claim.call_count, 3)
        self.assertEqual(batch.members(), [])


class TestClaimBatchPrecheck(BaseClaimBatchTest):

    def _assert_nothing_created(self):
        self.mock_k8s_helper.create_batch_lease.assert_not_called()
        self.mock_k8s_helper.create_sandbox_claim.assert_not_called()

    def test_missing_warmpool_raises_before_lease(self):
        self.mock_k8s_helper.get_sandbox_warmpool.side_effect = SandboxWarmPoolNotFoundError("gone")
        with self.assertRaises(SandboxWarmPoolNotFoundError):
            self._claim_batch()
        self._assert_nothing_created()

    def test_missing_template_raises_before_lease(self):
        self.mock_k8s_helper.get_sandbox_template.side_effect = SandboxTemplateNotFoundError("gone")
        with self.assertRaises(SandboxTemplateNotFoundError):
            self._claim_batch()
        self._assert_nothing_created()

    def test_warmpool_without_template_ref_raises_template_not_found(self):
        self.mock_k8s_helper.get_sandbox_warmpool.return_value = {"spec": {}}
        with self.assertRaises(SandboxTemplateNotFoundError):
            self._claim_batch()
        self._assert_nothing_created()

    def test_server_errors_propagate_unchanged(self):
        for method in ("get_sandbox_warmpool", "get_sandbox_template"):
            with self.subTest(method=method):
                self.mock_k8s_helper.reset_mock()
                self.mock_k8s_helper.get_sandbox_warmpool.return_value = {
                    "spec": {"sandboxTemplateRef": {"name": "tmpl"}}
                }
                self.mock_k8s_helper.get_sandbox_warmpool.side_effect = None
                error = _api_error(500)
                getattr(self.mock_k8s_helper, method).side_effect = error
                with self.assertRaises(k8s_client.ApiException) as ctx:
                    self._claim_batch()
                self.assertIs(ctx.exception, error)
                self._assert_nothing_created()
                getattr(self.mock_k8s_helper, method).side_effect = None

    def test_forbidden_precheck_is_skipped_and_the_batch_is_created(self):
        for method in ("get_sandbox_warmpool", "get_sandbox_template"):
            with self.subTest(method=method):
                self.mock_k8s_helper.reset_mock()
                getattr(self.mock_k8s_helper, method).side_effect = _api_error(403)
                with self.assertLogs(level="WARNING") as logs:
                    batch = self._claim_batch(batch_id="bwarm" if "warmpool" in method else "btmpl")
                self.assertTrue(any("403" in line for line in logs.output))
                self.mock_k8s_helper.create_batch_lease.assert_called_once()
                self._wait_for_creates(batch)
                self.assertEqual(self.mock_k8s_helper.create_sandbox_claim.call_count, 2)
                getattr(self.mock_k8s_helper, method).side_effect = None

    def test_forbidden_warmpool_precheck_skips_its_template_check(self):
        self.mock_k8s_helper.get_sandbox_warmpool.side_effect = _api_error(403)
        with self.assertLogs(level="WARNING"):
            self._claim_batch()
        self.mock_k8s_helper.get_sandbox_template.assert_not_called()

    def test_both_present_proceeds_and_template_comes_from_the_pool(self):
        self.mock_k8s_helper.get_sandbox_warmpool.return_value = {
            "spec": {"sandboxTemplateRef": {"name": "python-tmpl"}}
        }
        batch = self._claim_batch(
            groups=[BatchGroup(warmpool="pool-a", size=1), BatchGroup(warmpool="pool-b", size=1)],
            namespace="ns-a",
        )
        self.assertEqual(
            self.mock_k8s_helper.get_sandbox_warmpool.call_args_list,
            [call("pool-a", "ns-a"), call("pool-b", "ns-a")],
        )
        self.mock_k8s_helper.get_sandbox_template.assert_called_with("python-tmpl", "ns-a")
        self.mock_k8s_helper.create_batch_lease.assert_called_once()
        self._wait_for_creates(batch)


class TestClaimBatchLease(BaseClaimBatchTest):

    def _renew_interval(self, batch):
        batch._renew_stop = MagicMock()
        batch._renew_stop.wait.return_value = True
        batch._renew_loop()
        return batch._renew_stop.wait.call_args.args[0]

    def test_custom_durations_are_written_and_used(self):
        batch = self._claim_batch(lease_duration=90, work_budget=1200, quorum_timeout=300)
        namespace, lease = self.mock_k8s_helper.create_batch_lease.call_args.args
        self.assertEqual(namespace, "default")
        self.assertEqual(lease.metadata.name, "batch-b1")
        self.assertEqual(
            lease.metadata.labels, {BATCH_ID_LABEL: "b1", CREATED_BY_LABEL: "python-client"}
        )
        self.assertEqual(
            lease.metadata.annotations,
            {
                BATCH_LEASE_DURATION_ANNOTATION: "90",
                BATCH_WORK_BUDGET_ANNOTATION: "1200",
                BATCH_QUORUM_TIMEOUT_ANNOTATION: "300",
            },
        )
        self.assertEqual(lease.spec.lease_duration_seconds, 90)
        self.assertEqual(lease.spec.holder_identity, batch._holder_identity)
        self.assertLess(abs((lease.spec.renew_time - datetime.now(UTC)).total_seconds()), 5)
        self.assertEqual(lease.spec.acquire_time, lease.spec.renew_time)
        _stop(batch)
        self.assertEqual(self._renew_interval(batch), 30)

    def test_defaults(self):
        batch = self._claim_batch()
        _, lease = self.mock_k8s_helper.create_batch_lease.call_args.args
        self.assertEqual(lease.spec.lease_duration_seconds, 60)
        self.assertEqual(
            lease.metadata.annotations,
            {
                BATCH_LEASE_DURATION_ANNOTATION: "60",
                BATCH_WORK_BUDGET_ANNOTATION: "3600",
                BATCH_QUORUM_TIMEOUT_ANNOTATION: "600",
            },
        )
        _stop(batch)
        self.assertEqual(self._renew_interval(batch), 20)


@patch.object(SandboxBatch, "_start", lambda self: None)
class TestAttachBudgetAnnotations(BaseBatchClientTest):

    def _attach(self, annotations):
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(
            holder_identity=None, renew_time=datetime.now(UTC), annotations=annotations
        )
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = ([_claim("b1-0")], "5")
        return self.client.get_batch("b1")

    def test_restores_claim_batch_durations(self):
        batch = self._attach(
            {
                BATCH_LEASE_DURATION_ANNOTATION: "90",
                BATCH_WORK_BUDGET_ANNOTATION: "1200",
                BATCH_QUORUM_TIMEOUT_ANNOTATION: "300",
            }
        )
        self.assertEqual(batch._lease_duration, 90)
        self.assertEqual(batch._quorum_timeout, 300)
        written = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written.spec.lease_duration_seconds, 90)

    def test_missing_annotations_fall_back_to_defaults(self):
        batch = self._attach({})
        self.assertEqual(batch._quorum_timeout, BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS)

    def test_invalid_annotations_raise_before_any_write(self):
        for bad in ("abc", "0"):
            with self.subTest(bad=bad):
                self.mock_k8s_helper.reset_mock()
                with self.assertRaises(BatchError):
                    self._attach({BATCH_QUORUM_TIMEOUT_ANNOTATION: bad})
                self.mock_k8s_helper.replace_batch_lease.assert_not_called()


class TestCreateManifests(BaseClaimBatchTest):

    def test_names_annotations_and_labels_per_group(self):
        batch = self._claim_batch(
            groups=[
                BatchGroup(warmpool="pool-z", size=2, min_ready=1),
                BatchGroup(warmpool="pool-a", size=1),
            ],
            labels={"team": "rl"},
        )
        self._wait_for_creates(batch)
        created = sorted(
            (c.args[0], c.args[1], c.kwargs["annotations"], c.kwargs["labels"])
            for c in self.mock_k8s_helper.create_sandbox_claim.call_args_list
        )
        labels = {"team": "rl", BATCH_ID_LABEL: "b1"}
        z = {BATCH_GROUP_SIZE_ANNOTATION: "2", BATCH_GROUP_MIN_READY_ANNOTATION: "1"}
        a = {BATCH_GROUP_SIZE_ANNOTATION: "1", BATCH_GROUP_MIN_READY_ANNOTATION: "1"}
        self.assertEqual(
            created,
            [("b1-0", "pool-z", z, labels), ("b1-1", "pool-z", z, labels), ("b1-2", "pool-a", a, labels)],
        )

    def test_shutdown_time_is_each_claims_own_create_time_plus_budget(self):
        handle = _make_handle(MagicMock(), work_budget=100, quorum_timeout=50)
        group = BatchGroup(warmpool="pool-a", size=2)
        created = []
        handle._client.k8s_helper.create_sandbox_claim.side_effect = (
            lambda *a, **k: created.append(k["lifecycle"])
        )
        first = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        second = first + timedelta(minutes=30)
        with patch("k8s_agent_sandbox.utils.datetime") as mock_dt:
            mock_dt.now.return_value = first
            handle._create_with_retry("b1-0", group)
            mock_dt.now.return_value = second
            handle._create_with_retry("b1-1", group)
        budget = timedelta(seconds=50 + 100 + 600)
        self.assertEqual(
            [c["shutdownTime"] for c in created],
            [(first + budget).strftime("%Y-%m-%dT%H:%M:%SZ"), (second + budget).strftime("%Y-%m-%dT%H:%M:%SZ")],
        )
        self.assertEqual({c["shutdownPolicy"] for c in created}, {"Delete"})


class TestCreatePacing(unittest.TestCase):

    def test_no_more_than_create_rps_starts_in_any_second(self):
        clock = _FakeClock()
        handle, plan = _planned_handle(
            MagicMock(), [BatchGroup(warmpool="pool-a", size=35)], clock=clock, create_rps=10.0, max_in_flight=1
        )
        starts = []
        handle._client.k8s_helper.create_sandbox_claim.side_effect = lambda *a, **k: starts.append(clock.now)
        handle._wait_for_stop = lambda seconds: (clock.advance(seconds), False)[1]

        handle._run_creates(plan)

        self.assertEqual(len(starts), 35)
        for i, first in enumerate(starts):
            self.assertLessEqual(len([t for t in starts[i:] if t < first + 1.0 - 1e-9]), 10)

    def test_never_more_than_max_in_flight_concurrent_creates(self):
        handle, plan = _planned_handle(
            MagicMock(), [BatchGroup(warmpool="pool-a", size=12)], create_rps=1e6, max_in_flight=3
        )
        lock = threading.Lock()
        state = {"current": 0, "max": 0}
        saturated = threading.Event()
        gate = threading.Event()

        def create(*args, **kwargs):
            with lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
                if state["current"] == 3:
                    saturated.set()
            gate.wait(5)
            with lock:
                state["current"] -= 1

        handle._client.k8s_helper.create_sandbox_claim.side_effect = create
        handle._wait_for_stop = lambda seconds: False
        producer = threading.Thread(target=handle._run_creates, args=(plan,))
        producer.start()
        self.assertTrue(saturated.wait(5))
        gate.set()
        producer.join(5)
        self.assertEqual(state["max"], 3)
        self.assertEqual(handle._client.k8s_helper.create_sandbox_claim.call_count, 12)


class TestCreateRetry(unittest.TestCase):

    def setUp(self):
        self.handle = _make_handle(MagicMock())
        self.create = self.handle._client.k8s_helper.create_sandbox_claim
        self.sleeps = []
        self.handle._wait_for_stop = lambda seconds: (self.sleeps.append(seconds), False)[1]
        self.group = BatchGroup(warmpool="pool-a", size=1)

    def _run(self, *side_effects):
        self.create.side_effect = list(side_effects)
        return self.handle._create_with_retry("b1-0", self.group)

    def test_429_and_503_retry_then_succeed(self):
        for status in (429, 503):
            with self.subTest(status=status):
                self.create.reset_mock()
                self.assertIsNone(self._run(_api_error(status), None))
                self.assertEqual(self.create.call_count, 2)

    def test_transport_error_retries(self):
        self.assertIsNone(self._run(urllib3.exceptions.ProtocolError("reset"), None))
        self.assertEqual(self.create.call_count, 2)

    def test_409_after_retried_503_counts_as_success(self):
        self.assertIsNone(self._run(_api_error(503), _api_error(409)))

    def test_409_on_first_attempt_fails(self):
        error = self._run(_api_error(409))
        self.assertEqual(error.status, 409)
        self.assertEqual(self.create.call_count, 1)

    def test_client_errors_fail_without_retry(self):
        for status in (400, 403, 404, 422):
            with self.subTest(status=status):
                self.create.reset_mock()
                self.assertEqual(self._run(_api_error(status)).status, status)
                self.assertEqual(self.create.call_count, 1)

    def test_third_attempt_is_the_last(self):
        error = self._run(_api_error(503), _api_error(503), _api_error(503), None)
        self.assertEqual(error.status, 503)
        self.assertEqual(self.create.call_count, 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_retry_after_is_honored(self):
        self.assertIsNone(self._run(_api_error(429, headers={"Retry-After": "7"}), None))
        self.assertEqual(self.sleeps, [7.0])

    def test_exhausted_retries_become_a_create_failed_member(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=1)])
        handle._wait_for_stop = lambda seconds: False
        handle._client.k8s_helper.create_sandbox_claim.side_effect = _api_error(503)
        handle._in_flight_slots.acquire()  # _create_one releases the slot the producer took for it.
        handle._create_one("b1-0", handle._state.groups[0])
        member = handle.members()[0]
        self.assertTrue(member.terminal)
        self.assertEqual(member.reason, "CreateFailed")
        events = handle.events()
        event = next(events)
        self.assertEqual(event.type, BatchEventType.MEMBER_FAILED)
        self.assertEqual(event.member.claim_name, "b1-0")


class TestPerGroupFailFast(BaseClaimBatchTest):

    def test_unreachable_group_cancels_its_creates_while_other_group_fills(self):
        def create(name, warmpool, namespace, **kwargs):
            if warmpool == "pool-a":
                raise _api_error(403)

        # Creation waits for the watch, so holding the watch back lets quorum mode be fixed first.
        watch_gate = threading.Event()
        idle_watch = _idle_watch()

        def gated_watch(*args, **kwargs):
            watch_gate.wait(5)
            return idle_watch(*args, **kwargs)

        self.mock_k8s_helper.watch_sandbox_claims.side_effect = gated_watch
        self.mock_k8s_helper.create_sandbox_claim.side_effect = create
        batch = self._claim_batch(
            groups=[
                BatchGroup(warmpool="pool-a", size=4, min_ready=3),
                BatchGroup(warmpool="pool-b", size=2),
            ],
            max_in_flight=1,
        )
        groups = batch.iter_ready_groups()
        watch_gate.set()
        self._wait_for_creates(batch)

        created = [c.args[0] for c in self.mock_k8s_helper.create_sandbox_claim.call_args_list]
        self.assertEqual(created, ["b1-0", "b1-1", "b1-4", "b1-5"])
        self.assertEqual(
            [(m.claim_name, m.reason) for m in batch.members()],
            [("b1-0", "CreateFailed"), ("b1-1", "CreateFailed")],
        )

        first = next(groups)
        self.assertEqual(first.warmpool, "pool-a")
        self.assertIsInstance(first.error, QuorumUnreachableError)
        self.assertEqual(first.error.create_failed, 2)

        _apply(batch, _ready("b1-4", warmpool="pool-b"))
        _apply(batch, _ready("b1-5", warmpool="pool-b"))
        second = next(groups)
        self.assertEqual([m.claim_name for m in second.members], ["b1-4", "b1-5"])

        self.mock_k8s_helper.delete_sandbox_claim_collection.assert_not_called()
        self.mock_k8s_helper.delete_batch_lease.assert_not_called()
        self.assertIn(("default", "b1"), self.client._active_batches)


class TestFailFastConsumerMode(unittest.TestCase):

    def _handle(self, groups):
        clock = _FakeClock()
        handle, plan = _planned_handle(MagicMock(), groups, clock=clock, create_rps=1.0, max_in_flight=1)
        return handle, plan, clock

    def test_stream_only_batch_keeps_creating_after_a_create_failure(self):
        handle, plan, clock = self._handle([BatchGroup(warmpool="pool-a", size=3)])
        handle._wait_for_stop = lambda seconds: (clock.advance(seconds), False)[1]

        def create(name, *args, **kwargs):
            if name == "b1-0":
                raise _api_error(403)

        handle._client.k8s_helper.create_sandbox_claim.side_effect = create
        events = handle.events()
        handle._run_creates(plan)

        created = [c.args[0] for c in handle._client.k8s_helper.create_sandbox_claim.call_args_list]
        self.assertEqual(created, ["b1-0", "b1-1", "b1-2"])
        self.assertEqual([(m.claim_name, m.reason) for m in handle.members()], [("b1-0", "CreateFailed")])
        _apply(handle, _ready("b1-1"))
        _apply(handle, _ready("b1-2"))
        self.assertEqual(
            [(e.type, e.member.claim_name) for e in events],
            [
                (BatchEventType.MEMBER_FAILED, "b1-0"),
                (BatchEventType.MEMBER_READY, "b1-1"),
                (BatchEventType.MEMBER_READY, "b1-2"),
            ],
        )

    def test_entering_quorum_mode_cancels_a_group_already_past_its_threshold(self):
        handle, plan, clock = self._handle(
            [BatchGroup(warmpool="pool-a", size=4, min_ready=3), BatchGroup(warmpool="pool-b", size=2)]
        )
        holder = {}

        def sleep(seconds):
            clock.advance(seconds)
            # The producer paces before each create after the first; by the third, both of
            # pool-a's failures are recorded, still outside quorum mode.
            if len(handle._client.k8s_helper.create_sandbox_claim.call_args_list) == 2 and "groups" not in holder:
                self.assertEqual(handle._cancelled_pools, set())
                holder["groups"] = handle.iter_ready_groups()
            return False

        def create(name, warmpool, *args, **kwargs):
            if warmpool == "pool-a":
                raise _api_error(403)

        handle._wait_for_stop = sleep
        handle._client.k8s_helper.create_sandbox_claim.side_effect = create
        handle._run_creates(plan)

        created = [c.args[0] for c in handle._client.k8s_helper.create_sandbox_claim.call_args_list]
        self.assertEqual(created, ["b1-0", "b1-1", "b1-4", "b1-5"])
        self.assertEqual(
            [(m.claim_name, m.reason) for m in handle.members()],
            [("b1-0", "CreateFailed"), ("b1-1", "CreateFailed")],
        )
        verdict = next(holder["groups"])
        self.assertEqual(verdict.warmpool, "pool-a")
        self.assertIsInstance(verdict.error, QuorumUnreachableError)


class TestEvents(unittest.TestCase):

    def test_streams_and_closes_on_settle_with_mixed_outcome(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=4)])
        events = handle.events()
        _apply(handle, _ready("b1-0"))
        _apply(handle, _terminal("b1-1"))
        with handle._cond:
            handle._state.mark_create_failed("b1-2", "quota")
            handle._state.mark_released("b1-3")
        seen = [(e.type, e.member.claim_name) for e in events]
        self.assertEqual(
            seen,
            [
                (BatchEventType.MEMBER_READY, "b1-0"),
                (BatchEventType.MEMBER_FAILED, "b1-1"),
                (BatchEventType.MEMBER_FAILED, "b1-2"),
            ],
        )

    def test_member_lost_for_vanished_claim(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=1)])
        events = handle.events()
        _apply(handle, _claim("b1-0"))
        _apply(handle, lost="b1-0")
        self.assertEqual([(e.type, e.member.claim_name) for e in events], [(BatchEventType.MEMBER_LOST, "b1-0")])

    def test_closes_at_quorum_timeout_with_member_pending_forever(self):
        for quorum_first in (False, True):
            with self.subTest(quorum_first=quorum_first):
                clock = _FakeClock()
                handle, _ = _planned_handle(
                    MagicMock(), [BatchGroup(warmpool="pool-a", size=2, min_ready=1)], clock=clock
                )
                if quorum_first:
                    groups = handle.iter_ready_groups()
                events = handle.events()
                _apply(handle, _ready("b1-0"))
                _apply(handle, _claim("b1-1"))
                if quorum_first:
                    self.assertEqual([m.claim_name for m in next(groups).members], ["b1-0"])
                else:
                    self.assertEqual(next(events).member.claim_name, "b1-0")
                clock.advance(BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS)
                self.assertEqual(list(events), [])

    def test_waiting_consumer_wakes_at_deadline(self):
        handle, _ = _planned_handle(
            MagicMock(), [BatchGroup(warmpool="pool-a", size=1)], clock=time.monotonic, quorum_timeout=1
        )
        handle._state.set_fill_deadline(time.monotonic() + 0.05)
        self.assertEqual(list(handle.events()), [])

    def test_second_consumer_raises(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=1)])
        handle.events()
        with self.assertRaises(BatchError):
            handle.events()

    def test_lease_degraded_once_per_episode(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=1)])
        helper = handle._client.k8s_helper
        events = handle.events()
        helper.read_batch_lease.side_effect = RuntimeError("api down")
        handle._renew_once()
        handle._renew_once()
        helper.read_batch_lease.side_effect = None
        helper.read_batch_lease.return_value = _lease(holder_identity=handle._holder_identity)
        handle._renew_once()
        helper.read_batch_lease.side_effect = RuntimeError("api down again")
        handle._renew_once()
        _apply(handle, _ready("b1-0"))
        self.assertEqual(
            [e.type for e in events],
            [BatchEventType.LEASE_DEGRADED, BatchEventType.LEASE_DEGRADED, BatchEventType.MEMBER_READY],
        )


class TestIterReadyGroups(unittest.TestCase):

    def test_fast_group_first_extras_on_events_then_closes(self):
        handle, _ = _planned_handle(
            MagicMock(),
            [BatchGroup(warmpool="slow", size=2, min_ready=2), BatchGroup(warmpool="fast", size=3, min_ready=2)],
        )
        groups = handle.iter_ready_groups()
        events = handle.events()
        for name in ("b1-2", "b1-3", "b1-4"):
            _apply(handle, _ready(name, warmpool="fast"))
        fast = next(groups)
        self.assertEqual((fast.warmpool, [m.claim_name for m in fast.members]), ("fast", ["b1-2", "b1-3"]))
        self.assertEqual(next(events).member.claim_name, "b1-4")
        _apply(handle, _ready("b1-0", warmpool="slow"))
        _apply(handle, _ready("b1-1", warmpool="slow"))
        slow = next(groups)
        self.assertEqual([m.claim_name for m in slow.members], ["b1-0", "b1-1"])
        self.assertEqual(list(groups), [])
        self.assertEqual(list(events), [])

    def test_unreachable_group_errors_while_other_yields(self):
        handle, _ = _planned_handle(
            MagicMock(),
            [BatchGroup(warmpool="bad", size=2, min_ready=2), BatchGroup(warmpool="good", size=1)],
        )
        groups = handle.iter_ready_groups()
        _apply(handle, _terminal("b1-0", warmpool="bad"))
        _apply(handle, _ready("b1-2", warmpool="good"))
        results = {g.warmpool: g for g in groups}
        self.assertIsInstance(results["bad"].error, QuorumUnreachableError)
        self.assertEqual(results["bad"].members, [])
        self.assertEqual([m.claim_name for m in results["good"].members], ["b1-2"])

    def test_min_ready_zero_yields_immediately(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=2, min_ready=0)])
        self.assertEqual(list(handle.iter_ready_groups()), [batch_state.GroupReady(warmpool="pool-a")])

    def test_stuck_group_times_out_other_groups_unaffected(self):
        clock = _FakeClock()
        handle, _ = _planned_handle(
            MagicMock(),
            [BatchGroup(warmpool="stuck", size=2), BatchGroup(warmpool="ok", size=1)],
            clock=clock,
        )
        groups = handle.iter_ready_groups()
        _apply(handle, _ready("b1-2", warmpool="ok"))
        self.assertEqual(next(groups).warmpool, "ok")
        clock.advance(BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS)
        stuck = next(groups)
        self.assertEqual(stuck.warmpool, "stuck")
        self.assertIsInstance(stuck.error, TimeoutError)
        self.assertEqual(str(stuck.error), "Group quorum timed out")
        self.assertEqual(list(groups), [])

    def test_dependency_not_found_member_is_pending_until_timeout(self):
        clock = _FakeClock()
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=1)], clock=clock)
        groups = handle.iter_ready_groups()
        events = handle.events()
        _apply(handle, _terminal("b1-0", reason="WarmPoolNotFound"))
        with handle._lock:
            self.assertEqual(handle._state.pop_group_verdicts(), ([], False))
        clock.advance(BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS)
        self.assertIsInstance(next(groups).error, TimeoutError)
        self.assertEqual(list(events), [])

    def test_events_first_makes_iter_ready_groups_raise(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=1)])
        handle.events()
        with self.assertRaises(BatchError):
            handle.iter_ready_groups()

    def test_error_verdict_releases_held_members_to_events(self):
        handle, _ = _planned_handle(MagicMock(), [BatchGroup(warmpool="pool-a", size=3, min_ready=3)])
        groups = handle.iter_ready_groups()
        events = handle.events()
        _apply(handle, _ready("b1-0"))
        _apply(handle, _terminal("b1-1"))
        self.assertIsInstance(next(groups).error, QuorumUnreachableError)
        _apply(handle, _terminal("b1-2"))
        self.assertEqual(
            [(e.type, e.member.claim_name) for e in events],
            [
                (BatchEventType.MEMBER_FAILED, "b1-1"),
                (BatchEventType.MEMBER_READY, "b1-0"),
                (BatchEventType.MEMBER_FAILED, "b1-2"),
            ],
        )


@patch.object(SandboxBatch, "_start", lambda self: None)
class TestReattachedConsumers(BaseBatchClientTest):

    def test_group_already_at_min_ready_yields_without_blocking(self):
        annotations = {BATCH_GROUP_SIZE_ANNOTATION: "3", BATCH_GROUP_MIN_READY_ANNOTATION: "2"}
        claims = [
            {**_ready(name), "metadata": {"name": name, "annotations": annotations}}
            for name in ("b1-2", "b1-0", "b1-1")
        ]
        self.mock_k8s_helper.read_batch_lease.return_value = _lease(renew_time=datetime.now(UTC))
        self.mock_k8s_helper.list_sandbox_claim_objects.return_value = (claims, "5")
        batch = self.client.get_batch("b1")

        groups = batch.iter_ready_groups()
        events = batch.events()
        ready = next(groups)
        self.assertEqual([m.claim_name for m in ready.members], ["b1-0", "b1-1"])
        self.assertEqual(list(groups), [])
        self.assertEqual([e.member.claim_name for e in events], ["b1-2"])


class TestRelease(unittest.TestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.helper = self.mock_client.k8s_helper
        self.helper.list_sandbox_claim_objects.return_value = ([], "9")
        self.handle, _ = _planned_handle(self.mock_client, [BatchGroup(warmpool="pool-a", size=2)])

    def test_deletes_by_exact_label_then_lease_last(self):
        order = MagicMock()
        order.attach_mock(self.helper.delete_sandbox_claim_collection, "deletecollection")
        order.attach_mock(self.helper.list_sandbox_claim_objects, "list")
        order.attach_mock(self.helper.delete_batch_lease, "delete_lease")
        self.handle.release()
        self.assertEqual(
            order.mock_calls,
            [
                call.deletecollection("default", f"{BATCH_ID_LABEL}=b1"),
                call.list("default", f"{BATCH_ID_LABEL}=b1"),
                call.delete_lease("batch-b1", "default"),
            ],
        )
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")

    @patch("k8s_agent_sandbox.sandbox_batch.time.sleep")
    def test_relists_until_only_terminating_claims_remain(self, mock_sleep):
        live = {"metadata": {"name": "b1-0"}}
        terminating = {"metadata": {"name": "b1-0", "deletionTimestamp": "2026-01-01T00:00:00Z"}}
        self.helper.list_sandbox_claim_objects.side_effect = [([live], "9"), ([terminating], "10")]
        self.handle.release()
        self.assertEqual(self.helper.delete_sandbox_claim_collection.call_count, 2)
        self.helper.delete_batch_lease.assert_called_once()

    @patch("k8s_agent_sandbox.sandbox_batch.time.sleep")
    def test_relist_loop_is_bounded(self, mock_sleep):
        self.helper.list_sandbox_claim_objects.return_value = ([{"metadata": {"name": "b1-0"}}], "9")
        with self.assertRaises(BatchError):
            self.handle.release()
        self.assertEqual(
            self.helper.delete_sandbox_claim_collection.call_count, BATCH_RELEASE_MAX_DELETE_ROUNDS
        )
        self.helper.delete_batch_lease.assert_not_called()

    def test_idempotent(self):
        self.handle.release()
        self.handle.release()
        self.helper.delete_sandbox_claim_collection.assert_called_once()
        self.helper.delete_batch_lease.assert_called_once()
        self.mock_client._unregister_batch.assert_called_once()

    def test_wakes_waiters_ends_events_and_closes_connections(self):
        groups = self.handle.iter_ready_groups()
        events = self.handle.events()
        sandbox = MagicMock()
        self.handle._connected["b1-0"] = sandbox
        outcome = {}

        def wait_for_group():
            try:
                next(groups)
            except BatchError as e:
                outcome["error"] = e

        waiter = threading.Thread(target=wait_for_group)
        waiter.start()
        self.handle.release()
        waiter.join(5)
        self.assertFalse(waiter.is_alive())
        self.assertIsInstance(outcome.get("error"), BatchError)
        self.assertEqual(list(events), [])
        sandbox.close_connection.assert_called_once()
        with self.assertRaises(BatchError):
            self.handle.events()
        with self.assertRaises(BatchError):
            self.handle.detach()

    def test_cancels_outstanding_creates(self):
        plan = [(f"b1-{i}", BatchGroup(warmpool="pool-a", size=6)) for i in range(6)]
        handle = _make_handle(self.mock_client, groups=[BatchGroup(warmpool="pool-a", size=6)], create_rps=1.0)
        handle._state.plan_initial_fill()
        first_create = threading.Event()
        self.helper.create_sandbox_claim.side_effect = lambda *a, **k: first_create.set()
        handle._start_creation(plan)
        self.assertTrue(first_create.wait(5))
        handle.release()
        self.assertFalse(handle._create_thread.is_alive())
        self.assertLess(self.helper.create_sandbox_claim.call_count, 6)

    def test_hung_create_does_not_block_release(self):
        handle = _make_handle(self.mock_client, groups=[BatchGroup(warmpool="pool-a", size=2)])
        plan = handle._state.plan_initial_fill()
        started, gate = threading.Event(), threading.Event()

        def create(*args, **kwargs):
            started.set()
            gate.wait(10)

        self.helper.create_sandbox_claim.side_effect = create
        handle._start_creation(plan)
        self.assertTrue(started.wait(5))
        try:
            with patch("k8s_agent_sandbox.sandbox_batch.BATCH_STOP_CREATION_TIMEOUT_SECONDS", 0.05), \
                 self.assertLogs(level="WARNING") as logs:
                handle.release()
            self.assertTrue(any("in flight" in line for line in logs.output))
            self.helper.delete_sandbox_claim_collection.assert_called_once()
            self.helper.delete_batch_lease.assert_called_once()
        finally:
            gate.set()
            handle._create_thread.join(5)

    def test_403_propagates_keeps_lease_and_registration_then_retry_succeeds(self):
        groups = self.handle.iter_ready_groups()
        events = self.handle.events()
        forbidden = _api_error(403)
        self.helper.delete_sandbox_claim_collection.side_effect = forbidden
        with self.assertRaises(k8s_client.ApiException) as ctx:
            self.handle.release()
        self.assertIs(ctx.exception, forbidden)
        self.helper.delete_batch_lease.assert_not_called()
        self.mock_client._unregister_batch.assert_not_called()
        self.assertEqual(list(events), [])
        with self.assertRaises(BatchError):
            next(groups)

        self.helper.delete_sandbox_claim_collection.side_effect = None
        self.handle.release()
        self.helper.delete_batch_lease.assert_called_once_with("batch-b1", "default")
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")

    def test_release_after_detach_raises(self):
        self.helper.read_batch_lease.return_value = _lease(holder_identity=self.handle._holder_identity)
        self.handle.detach()
        with self.assertRaises(BatchError):
            self.handle.release()


class TestDetachEndsConsumers(unittest.TestCase):

    def test_detach_ends_events_and_wakes_group_waiters(self):
        mock_client = MagicMock()
        handle, _ = _planned_handle(mock_client, [BatchGroup(warmpool="pool-a", size=1)])
        mock_client.k8s_helper.read_batch_lease.return_value = _lease(holder_identity=handle._holder_identity)
        groups = handle.iter_ready_groups()
        events = handle.events()
        handle.detach()
        self.assertEqual(list(events), [])
        with self.assertRaises(BatchError):
            next(groups)
        mock_client.k8s_helper.delete_sandbox_claim_collection.assert_not_called()


class TestExitHooks(unittest.TestCase):

    @patch("k8s_agent_sandbox.sandbox_client.K8sHelper")
    def test_delete_all_releases_tracked_batches_and_skips_detached(self, MockK8sHelper):
        client = SandboxClient()
        live = MagicMock(_detached=False)
        detached = MagicMock(_detached=True)
        failing = MagicMock(_detached=False)
        failing.release.side_effect = RuntimeError("boom")
        client._active_batches = {("ns", "b1"): live, ("ns", "b2"): detached, ("ns", "b3"): failing}
        client.delete_all()
        live.release.assert_called_once()
        detached.release.assert_not_called()
        failing.release.assert_called_once()


if __name__ == "__main__":
    unittest.main()
