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
"""Unit tests for the async AsyncSandboxBatch handle and AsyncSandboxClient.get_batch."""

import asyncio
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("kubernetes_asyncio")

from kubernetes_asyncio import client as k8s_client

from k8s_agent_sandbox import batch_state
from k8s_agent_sandbox.async_sandbox_batch import AsyncSandboxBatch
from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.exceptions import (
    BatchError,
    BatchInUseError,
    BatchLeaseExpiredError,
    BatchNotFoundError,
    SandboxNotReadyError,
)
from k8s_agent_sandbox.models import BatchGroup, SandboxDirectConnectionConfig


def _lease(
    holder_identity=None,
    renew_time=None,
    lease_duration_seconds=60,
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


async def _agen(events):
    for event in events:
        yield event


async def _agen_then_raise(events, exc):
    for event in events:
        yield event
    raise exc


def _make_handle(client, batch_id="b1", namespace="default", groups=None, lease_duration=60):
    groups = groups if groups is not None else [BatchGroup(warmpool="pool-a", size=1)]
    state = batch_state.BatchState(batch_id, groups)
    return AsyncSandboxBatch(
        client=client,
        batch_id=batch_id,
        namespace=namespace,
        state=state,
        lease_name=f"batch-{batch_id}",
        holder_identity="host_1234_abcd1234",
        lease_duration=lease_duration,
        list_resource_version="5",
    )


class BaseAsyncBatchClientTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        patcher = patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper")
        self.MockAsyncK8sHelper = patcher.start()
        self.addCleanup(patcher.stop)

        config = SandboxDirectConnectionConfig(api_url="http://test-router:8080")
        self.client = AsyncSandboxClient(connection_config=config, cleanup=False)
        self.mock_k8s_helper = self.client.k8s_helper
        self.mock_sandbox_class = MagicMock()
        self.client.sandbox_class = self.mock_sandbox_class


@patch.object(AsyncSandboxBatch, "_start", lambda self: None)
class TestGetBatchLeaseCases(BaseAsyncBatchClientTest):

    async def test_not_found_raises(self):
        self.mock_k8s_helper.read_batch_lease = AsyncMock(return_value=None)
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(return_value=([], "0"))
        with self.assertRaises(BatchNotFoundError):
            await self.client.get_batch("b1")

    async def test_stale_held_lease_raises_expired(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(holder_identity="someone-else", renew_time=now - timedelta(seconds=120))
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        with self.assertRaises(BatchLeaseExpiredError):
            await self.client.get_batch("b1")

    async def test_missing_lease_with_claims_present_raises_expired_and_creates_nothing(self):
        self.mock_k8s_helper.read_batch_lease = AsyncMock(return_value=None)
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        with self.assertRaises(BatchLeaseExpiredError):
            await self.client.get_batch("b1")
        self.mock_k8s_helper.replace_batch_lease.assert_not_called()

    async def test_unheld_but_stale_lease_raises_expired(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(holder_identity=None, renew_time=now - timedelta(seconds=120))
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        with self.assertRaises(BatchLeaseExpiredError):
            await self.client.get_batch("b1")

    async def test_skew_margin_boundary_adoptable_then_expired_one_second_later(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()

        with patch("k8s_agent_sandbox.async_sandbox_batch.datetime") as mock_dt:
            mock_dt.now.return_value = now
            self.mock_k8s_helper.read_batch_lease = AsyncMock(
                return_value=_lease(holder_identity=None, renew_time=now - timedelta(seconds=55))
            )
            self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
                return_value=([_claim("b1-0")], "5")
            )
            batch = await self.client.get_batch("b1")
            self.assertIsNotNone(batch)

        with patch("k8s_agent_sandbox.async_sandbox_batch.datetime") as mock_dt2:
            mock_dt2.now.return_value = now
            self.mock_k8s_helper.read_batch_lease = AsyncMock(
                return_value=_lease(holder_identity=None, renew_time=now - timedelta(seconds=56))
            )
            with self.assertRaises(BatchLeaseExpiredError):
                await self.client.get_batch("b1")

    async def test_get_batch_rejects_adopt_expired_kwarg(self):
        with self.assertRaises(TypeError):
            await self.client.get_batch("b1", adopt_expired=True)

    async def test_live_different_holder_raises_in_use(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(holder_identity="other-holder", renew_time=now)
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        with self.assertRaises(BatchInUseError):
            await self.client.get_batch("b1")

    async def test_live_unheld_takes_over(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(holder_identity=None, renew_time=now)
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=(
                [_claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})],
                "5",
            )
        )
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        batch = await self.client.get_batch("b1")
        self.assertEqual(batch.batch_id, "b1")
        self.mock_k8s_helper.replace_batch_lease.assert_called_once()


@patch.object(AsyncSandboxBatch, "_start", lambda self: None)
class TestTakeoverWrites(BaseAsyncBatchClientTest):

    async def test_writes_holder_identity_and_renew_time_near_now(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(holder_identity=None, renew_time=now)
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        await self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertIsNotNone(written_lease.spec.holder_identity)
        self.assertLess(
            abs((written_lease.spec.renew_time - datetime.now(UTC)).total_seconds()), 5
        )

    async def test_annotation_90_with_stale_spec_field_writes_90_and_renews_every_30(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(
                holder_identity=None,
                renew_time=now,
                lease_duration_seconds=300,
                annotations={"agents.x-k8s.io/batch-lease-duration": "90"},
            )
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        batch = await self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written_lease.spec.lease_duration_seconds, 90)
        self.assertEqual(batch._lease_duration, 90)
        self.assertEqual(max(1, batch._lease_duration // 3), 30)

    async def test_no_annotation_defaults_to_60_and_renews_every_20(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(holder_identity=None, renew_time=now)
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        batch = await self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written_lease.spec.lease_duration_seconds, 60)
        self.assertEqual(max(1, batch._lease_duration // 3), 20)

    async def test_invalid_annotations_raise_batch_error_with_no_write(self):
        now = datetime.now(UTC)
        for bad in ("abc", "1.5", "0", "-5", "5"):
            with self.subTest(bad=bad):
                self.mock_k8s_helper.replace_batch_lease = AsyncMock()
                self.mock_k8s_helper.read_batch_lease = AsyncMock(
                    return_value=_lease(
                        holder_identity=None,
                        renew_time=now,
                        annotations={"agents.x-k8s.io/batch-lease-duration": bad},
                    )
                )
                self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
                    return_value=([_claim("b1-0")], "5")
                )
                with self.assertRaises(BatchError):
                    await self.client.get_batch("b1")
                self.mock_k8s_helper.replace_batch_lease.assert_not_called()

    async def test_annotation_of_6_succeeds(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(
                holder_identity=None,
                renew_time=now,
                annotations={"agents.x-k8s.io/batch-lease-duration": "6"},
            )
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        await self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written_lease.spec.lease_duration_seconds, 6)

    async def test_takeover_leaves_annotation_unchanged(self):
        now = datetime.now(UTC)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(
                holder_identity=None,
                renew_time=now,
                annotations={"agents.x-k8s.io/batch-lease-duration": "90"},
            )
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim("b1-0")], "5")
        )
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        await self.client.get_batch("b1")

        written_lease = self.mock_k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(
            written_lease.metadata.annotations, {"agents.x-k8s.io/batch-lease-duration": "90"}
        )


class TestWatcher(BaseAsyncBatchClientTest):

    @staticmethod
    def _terminating_error(status=403):
        return k8s_client.ApiException(status=status)

    async def test_resumes_from_list_resource_version(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.watch_sandbox_claims = MagicMock(
            side_effect=[self._terminating_error()]
        )
        await handle._watch_loop()
        first_call = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[0]
        self.assertEqual(first_call.args[2], "5")

    async def test_410_relists_and_diffs_vanished_claim_becomes_lost(self):
        handle = _make_handle(self.client)
        handle._state.apply_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.mock_k8s_helper.watch_sandbox_claims = MagicMock(
            side_effect=[self._terminating_error(status=410), self._terminating_error(status=403)]
        )
        self.mock_k8s_helper.list_sandbox_claim_objects = AsyncMock(return_value=([], "9"))
        await handle._watch_loop()
        self.assertTrue(handle._state.get("b1-0").lost)

    async def test_disconnect_reconnects_from_last_resource_version(self):
        handle = _make_handle(self.client)
        event = {
            "type": "MODIFIED",
            "object": {
                "metadata": {"name": "b1-0", "resourceVersion": "77"},
                "spec": {"warmPoolRef": {"name": "pool-a"}},
                "status": {},
            },
        }
        self.mock_k8s_helper.watch_sandbox_claims = MagicMock(
            side_effect=[
                _agen_then_raise([event], ConnectionError("boom")),
                self._terminating_error(status=403),
            ]
        )
        await handle._watch_loop()
        second_call = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[1]
        self.assertEqual(second_call.args[2], "77")

    async def test_bookmark_only_advances_resource_version(self):
        handle = _make_handle(self.client)
        bookmark_event = {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "42"}}}
        self.mock_k8s_helper.watch_sandbox_claims = MagicMock(
            side_effect=[_agen([bookmark_event]), self._terminating_error(status=403)]
        )
        await handle._watch_loop()
        self.assertEqual(len(handle._state.members()), 0)
        second_call = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[1]
        self.assertEqual(second_call.args[2], "42")

    async def test_403_surfaces_through_err(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.watch_sandbox_claims = MagicMock(
            side_effect=[self._terminating_error(status=403)]
        )
        await handle._watch_loop()
        err = await handle.err()
        self.assertIsInstance(err, k8s_client.ApiException)
        self.assertEqual(err.status, 403)

    async def test_label_selector_is_exact(self):
        handle = _make_handle(self.client, batch_id="b1234")
        self.mock_k8s_helper.watch_sandbox_claims = MagicMock(
            side_effect=[self._terminating_error(status=403)]
        )
        await handle._watch_loop()
        call_args = self.mock_k8s_helper.watch_sandbox_claims.call_args_list[0]
        self.assertEqual(call_args.args[1], "agents.x-k8s.io/batch-id=b1234")


class TestRenewal(BaseAsyncBatchClientTest):

    async def test_renews_every_max_1_duration_over_3(self):
        handle = _make_handle(self.client, lease_duration=60)
        self.assertEqual(max(1, handle._lease_duration // 3), 20)
        handle2 = _make_handle(self.client, lease_duration=2)
        self.assertEqual(max(1, handle2._lease_duration // 3), 1)

    async def test_renewal_uses_read_resource_version(self):
        handle = _make_handle(self.client)
        lease = _lease(holder_identity=handle._holder_identity, resource_version="123")
        self.mock_k8s_helper.read_batch_lease = AsyncMock(return_value=lease)
        self.mock_k8s_helper.replace_batch_lease = AsyncMock()
        await handle._renew_once()
        self.mock_k8s_helper.replace_batch_lease.assert_called_once_with(
            handle._lease_name, handle.namespace, lease
        )
        self.assertEqual(lease.metadata.resource_version, "123")

    async def test_first_failure_logs_degraded(self):
        handle = _make_handle(self.client)
        self.mock_k8s_helper.read_batch_lease = AsyncMock(side_effect=RuntimeError("api down"))
        with self.assertLogs("k8s_agent_sandbox.async_sandbox_batch", level="INFO") as ctx:
            await handle._renew_once()
        self.assertTrue(any("degraded" in msg for msg in ctx.output))
        self.assertTrue(handle._renewal_degraded)

    async def test_no_success_for_lease_duration_returns_expired_and_sticks(self):
        handle = _make_handle(self.client, lease_duration=60)
        handle._last_renew_success = time.monotonic() - 61
        self.mock_k8s_helper.read_batch_lease = AsyncMock(side_effect=RuntimeError("api down"))
        await handle._renew_once()
        first_err = await handle.err()
        self.assertIsInstance(first_err, BatchLeaseExpiredError)
        await handle._renew_once()
        self.assertIs(await handle.err(), first_err)


class TestMembers(unittest.IsolatedAsyncioTestCase):

    async def test_warmpool_filter_and_snapshot_immutability(self):
        client_mock = MagicMock()
        handle = _make_handle(client_mock)
        handle._state.apply_claim(_claim("b1-0", warmpool="pool-a"))
        handle._state.apply_claim(_claim("b1-1", warmpool="pool-b"))

        pool_a = await handle.members(warmpool="pool-a")
        self.assertEqual([m.claim_name for m in pool_a], ["b1-0"])

        first = (await handle.members())[0]
        handle._state.apply_claim(
            _claim("b1-0", warmpool="pool-a", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        self.assertFalse(first.ready)


class TestConnect(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_sandbox_instance = MagicMock()
        self.mock_client.sandbox_class.return_value = self.mock_sandbox_instance
        self.handle = _make_handle(self.mock_client)

    async def test_builds_sandbox_class_without_resolve_or_get_sandbox(self):
        self.handle._state.apply_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = (await self.handle.members())[0]
        sandbox = await self.handle.connect(member)

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

    async def test_repeat_call_returns_same_handle(self):
        self.handle._state.apply_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = (await self.handle.members())[0]
        first = await self.handle.connect(member)
        second = await self.handle.connect(member)
        self.assertIs(first, second)
        self.mock_client.sandbox_class.assert_called_once()

    async def test_not_ready_member_raises(self):
        self.handle._state.apply_claim(_claim("b1-0", conditions=[]))
        member = (await self.handle.members())[0]
        with self.assertRaises(SandboxNotReadyError):
            await self.handle.connect(member)

    async def test_connected_handle_not_registered_in_active_connection_sandboxes(self):
        patcher = patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper")
        patcher.start()
        self.addCleanup(patcher.stop)
        config = SandboxDirectConnectionConfig(api_url="http://test-router:8080")
        real_client = AsyncSandboxClient(connection_config=config, cleanup=False)
        real_client.sandbox_class = MagicMock(return_value=MagicMock())
        handle = _make_handle(real_client)
        handle._state.apply_claim(
            _claim("b1-0", conditions=[{"type": "Ready", "status": "True"}], sandbox={"name": "sbx-1"})
        )
        member = (await handle.members())[0]
        await handle.connect(member)
        self.assertEqual(real_client._active_connection_sandboxes, {})


class TestDetach(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_client = MagicMock()
        self.mock_client._unregister_batch = MagicMock()
        self.handle = _make_handle(self.mock_client)

    async def test_invalid_grace_values_raise_value_error_leave_handle_running(self):
        self.mock_client.k8s_helper.replace_batch_lease = AsyncMock()
        for bad in (1.5, 30.0, True, "30", 0, -5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    await self.handle.detach(grace=bad)
                self.mock_client.k8s_helper.replace_batch_lease.assert_not_called()
                self.assertFalse(self.handle._detached)

    async def test_grace_of_5_raises_exact_message(self):
        with self.assertRaises(ValueError) as ctx:
            await self.handle.detach(grace=5)
        self.assertEqual(
            str(ctx.exception), "Duration must be greater than clock skew margin (5s)"
        )

    async def test_grace_of_6_is_accepted(self):
        self.mock_client.k8s_helper.read_batch_lease = AsyncMock(return_value=_lease())
        self.mock_client.k8s_helper.replace_batch_lease = AsyncMock()
        await self.handle.detach(grace=6)
        written = self.mock_client.k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written.spec.lease_duration_seconds, 6)

    async def test_final_write_clears_holder_sets_renew_time_and_grace_duration(self):
        self.mock_client.k8s_helper.read_batch_lease = AsyncMock(
            return_value=_lease(
                holder_identity="me", annotations={"agents.x-k8s.io/batch-lease-duration": "60"}
            )
        )
        self.mock_client.k8s_helper.replace_batch_lease = AsyncMock()
        await self.handle.detach(grace=10)
        written = self.mock_client.k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertIsNone(written.spec.holder_identity)
        self.assertLess(abs((written.spec.renew_time - datetime.now(UTC)).total_seconds()), 5)
        self.assertEqual(written.spec.lease_duration_seconds, 10)
        self.assertEqual(
            written.metadata.annotations, {"agents.x-k8s.io/batch-lease-duration": "60"}
        )

    async def test_grace_none_uses_handles_lease_duration(self):
        self.mock_client.k8s_helper.read_batch_lease = AsyncMock(return_value=_lease())
        self.mock_client.k8s_helper.replace_batch_lease = AsyncMock()
        handle = _make_handle(self.mock_client, lease_duration=90)
        await handle.detach(grace=None)
        written = self.mock_client.k8s_helper.replace_batch_lease.call_args.args[2]
        self.assertEqual(written.spec.lease_duration_seconds, 90)

    async def test_idempotent_closes_handles_unregisters_later_calls_raise(self):
        self.mock_client.k8s_helper.read_batch_lease = AsyncMock(return_value=_lease())
        self.mock_client.k8s_helper.replace_batch_lease = AsyncMock()
        mock_sandbox = MagicMock()
        mock_sandbox.close_connection = AsyncMock()
        self.handle._connected["b1-0"] = mock_sandbox

        await self.handle.detach()

        mock_sandbox.close_connection.assert_called_once()
        self.mock_client._unregister_batch.assert_called_once_with("default", "b1")
        self.assertEqual(self.handle._connected, {})

        with self.assertRaises(BatchError):
            await self.handle.detach()


class TestStart(unittest.IsolatedAsyncioTestCase):

    async def test_start_launches_two_background_tasks(self):
        mock_client = MagicMock()
        handle = _make_handle(mock_client)
        with patch.object(AsyncSandboxBatch, "_watch_loop", new=AsyncMock(return_value=None)), \
             patch.object(AsyncSandboxBatch, "_renew_loop", new=AsyncMock(return_value=None)):
            handle._start()
            await asyncio.wait_for(handle._watch_task, timeout=2)
            await asyncio.wait_for(handle._renew_task, timeout=2)
        self.assertTrue(handle._watch_task.done())
        self.assertTrue(handle._renew_task.done())


class TestClientClose(BaseAsyncBatchClientTest):

    async def test_close_stops_tracked_batches_tasks(self):
        self.mock_k8s_helper.close = AsyncMock()
        handle = _make_handle(self.client)
        handle._stop_background_tasks = AsyncMock()
        self.client._active_batches[("default", "b1")] = handle

        await self.client.close()

        handle._stop_background_tasks.assert_called_once()
        self.assertEqual(self.client._active_batches, {})


if __name__ == "__main__":
    unittest.main()
