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

"""12-Cell Subsystem Chaos and Fault-Injection Suite for SandboxBatch,
AsyncSandboxBatch, AsyncK8sHelper, AsyncSandboxClient, and batch_reaper.

Implements the SDK cells of Section 2.1 of comprehensive_verification_strategy.md
plus live reproductions for all uncovered SDK production findings:
  - Cell 01: Watch fatal 403 vs Lease renewal authority separation (F-01: INV-AUTH-1, INV-AUTH-2)
             and AsyncK8sHelper watch non-2xx HTTP status handling (F-09: INV-WATCH-HTTP-1)
  - Cell 02: Watch 410 Gone during active deletes + late watch ADDED after release_not_ready
             (F-03: INV-OWN-4, INV-LEAK-1)
  - Cell 03: Watch TLS SSLEOFError (transient retry) vs SSLCertVerificationError (fatal)
  - Cell 04: Watch short-connection exponential backoff + healthy stream reset
  - Cell 05: Renewal 409 Conflict same-holder recovery vs foreign takeover (INV-OWN-1)
  - Cell 06: Renewal 503 degraded state, recovery, and deadline self-fencing (INV-AUTH-3)
  - Cell 07: Renewal 404 external Lease deletion and TLS verification error
  - Cell 08: Create 429 Retry-After global cooldown and warm-credit pause
  - Cell 09: Dynamic acquire warm-pool 404 precheck eviction and recovery
  - Cell 10: Dynamic acquire missing template and terminal Ready=False claim failure
  - Cell 11: Dynamic acquire cancellation cleanup, retry backoff wake on release_group (F-26),
             and repeated cancellation under concurrent watch activity
  - Cell 12: Teardown connection-close retry, release_group Lease deadline clamping (F-04),
             AsyncSandboxClient.__aexit__ failed-batch retention (F-08), close vs release
             race (F-15), sync _watch_thread join (F-16), connect closed-cache / terminate
             Lease fence (F-17), batch_reaper 410 pagination / malformed renew_time isolation
             (F-18), in-flight acquire drain on release() (F-24), popped pacing slot cancellation
             on release_group (F-25), get_batch attach Lease fencing / pool_size clamp (F-27),
             and Lease replace 404 TOCTOU handling in release / get_batch / detach (F-29).
"""

from __future__ import annotations

import asyncio
import json
import ssl
import threading
import time
import unittest
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes import client as k8s_client
from kubernetes_asyncio import client as async_k8s_client

from k8s_agent_sandbox import batch_reaper
from k8s_agent_sandbox.async_k8s_helper import AsyncK8sHelper
from k8s_agent_sandbox.async_sandbox_batch import AsyncSandboxBatch
from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.batch_utils import BATCH_DEFAULT_LEASE_DURATION_SECONDS
from k8s_agent_sandbox.constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    BATCH_ID_LABEL,
    BATCH_LEASE_DURATION_ANNOTATION,
)
from k8s_agent_sandbox.exceptions import (
    BatchError,
    BatchInUseError,
    BatchLeaseExpiredError,
    BatchNotFoundError,
    SandboxTemplateNotFoundError,
    SandboxWarmPoolNotFoundError,
    TerminalMemberError,
)
from k8s_agent_sandbox.k8s_helper import K8sHelper
from k8s_agent_sandbox.models import (
    BatchGroup,
    MemberState,
    SandboxDirectConnectionConfig,
)
from k8s_agent_sandbox.sandbox_batch import SandboxBatch
from k8s_agent_sandbox.sandbox_client import SandboxClient


def _make_lease(
    holder_identity: str = "holder-1",
    resource_version: str = "10",
    uid: str = "lease-uid-1",
    lease_duration_seconds: int = BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    batch_id: str = "b1",
) -> k8s_client.V1Lease:
    return k8s_client.V1Lease(
        metadata=k8s_client.V1ObjectMeta(
            name=f"batch-{batch_id}",
            resource_version=resource_version,
            uid=uid,
            labels={BATCH_ID_LABEL: batch_id},
            annotations={},
        ),
        spec=k8s_client.V1LeaseSpec(
            holder_identity=holder_identity,
            renew_time=datetime.now(UTC),
            lease_duration_seconds=lease_duration_seconds,
            lease_transitions=0,
        ),
    )


def _make_async_lease(
    holder_identity: str = "holder-1",
    resource_version: str = "10",
    uid: str = "lease-uid-1",
    lease_duration_seconds: int = BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    batch_id: str = "b1",
) -> async_k8s_client.V1Lease:
    return async_k8s_client.V1Lease(
        metadata=async_k8s_client.V1ObjectMeta(
            name=f"batch-{batch_id}",
            resource_version=resource_version,
            uid=uid,
            labels={BATCH_ID_LABEL: batch_id},
            annotations={},
        ),
        spec=async_k8s_client.V1LeaseSpec(
            holder_identity=holder_identity,
            renew_time=datetime.now(UTC),
            lease_duration_seconds=lease_duration_seconds,
            lease_transitions=0,
        ),
    )


def _claim_obj(
    name: str,
    *,
    warmpool: str = "pool-a",
    ready: str = "True",
    reason: str = "SandboxReady",
    sandbox_name: str | None = "sbx-0",
    rv: str = "5",
    group_size: str = "1",
    min_ready: str = "1",
) -> dict[str, Any]:
    status: dict[str, Any] = {
        "conditions": [{"type": "Ready", "status": ready, "reason": reason, "message": reason}],
    }
    if sandbox_name is not None:
        status["sandbox"] = {"name": sandbox_name, "podIPs": ["10.0.0.2"]}
    return {
        "metadata": {
            "name": name,
            "resourceVersion": rv,
            "annotations": {
                BATCH_GROUP_SIZE_ANNOTATION: group_size,
                BATCH_GROUP_MIN_READY_ANNOTATION: min_ready,
            },
        },
        "spec": {"warmPoolRef": {"name": warmpool}},
        "status": status,
    }


def _new_stopped_sync_batch(
    groups: list[BatchGroup] | None = None,
    *,
    batch_id: str = "b1",
    lease_duration: int = 60,
    holder_identity: str = "h1",
    cleanup: bool = False,
) -> tuple[SandboxClient[Any], Any, SandboxBatch]:
    with (
        patch("k8s_agent_sandbox.sandbox_client.K8sHelper"),
        patch("k8s_agent_sandbox.sandbox_client.atexit.register"),
    ):
        client: SandboxClient[Any] = SandboxClient(cleanup=cleanup)
    helper: Any = client.k8s_helper
    helper.api_pool_size.return_value = 25
    helper.count_live_sandbox_claims.return_value = 0
    helper.get_sandbox_warmpool.return_value = {
        "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
        "status": {"readyReplicas": 2},
    }
    helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
    helper.create_batch_lease.return_value = _make_lease(
        holder_identity=holder_identity,
        resource_version="10",
        lease_duration_seconds=lease_duration,
        batch_id=batch_id,
    )
    helper.list_sandbox_claim_objects.return_value = ([], "5")
    client_any: Any = client
    client_any.sandbox_class = MagicMock(
        side_effect=lambda **kw: MagicMock(
            sandbox_id=kw.get("sandbox_id"),
            claim_name=kw.get("claim_name"),
            is_active=True,
            close_connection=MagicMock(),
        )
    )
    with (
        patch.object(SandboxBatch, "_start", lambda _self: None),
        patch.object(SandboxBatch, "_create_worker", lambda _self: None),
    ):
        batch = client.claim_batch(
            groups if groups is not None else [BatchGroup(warmpool="pool-a", size=1)],
            batch_id=batch_id,
            lease_duration=lease_duration,
        )
    batch._holder_identity = holder_identity
    assert batch._lease.spec is not None
    batch._lease.spec.holder_identity = holder_identity
    helper.read_batch_lease.side_effect = lambda *_a, **_kw: batch._lease
    helper.replace_batch_lease.side_effect = lambda _n, _ns, body, **_kw: _make_lease(
        holder_identity=body.spec.holder_identity,
        resource_version="99",
        lease_duration_seconds=body.spec.lease_duration_seconds or lease_duration,
        batch_id=batch_id,
    )
    return client, helper, batch


async def _new_stopped_async_batch(
    groups: list[BatchGroup] | None = None,
    *,
    batch_id: str = "b1",
    lease_duration: int = 60,
    holder_identity: str = "h1",
    cleanup: bool = False,
) -> tuple[AsyncSandboxClient[Any], Any, AsyncSandboxBatch]:
    with (
        patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper"),
        patch("k8s_agent_sandbox.async_sandbox_client.atexit.register"),
    ):
        client: AsyncSandboxClient[Any] = AsyncSandboxClient(
            connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
            cleanup=cleanup,
        )
    helper: Any = client.k8s_helper
    helper.api_pool_size = AsyncMock(return_value=25)
    helper.count_live_sandbox_claims = AsyncMock(return_value=0)
    helper.get_sandbox_warmpool = AsyncMock(
        return_value={
            "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
            "status": {"readyReplicas": 2},
        }
    )
    helper.get_sandbox_template = AsyncMock(return_value={"metadata": {"name": "tmpl"}})
    helper.create_batch_lease = AsyncMock(
        return_value=_make_async_lease(
            holder_identity=holder_identity,
            resource_version="10",
            lease_duration_seconds=lease_duration,
            batch_id=batch_id,
        )
    )
    helper.list_sandbox_claim_objects = AsyncMock(return_value=([], "5"))
    helper.delete_sandbox_claim = AsyncMock()
    helper.delete_sandbox_claims_by_label = AsyncMock()
    helper.delete_batch_lease = AsyncMock()
    helper.close = AsyncMock()
    client_any: Any = client
    client_any.sandbox_class = MagicMock(
        side_effect=lambda **kw: MagicMock(
            sandbox_id=kw.get("sandbox_id"),
            claim_name=kw.get("claim_name"),
            is_active=True,
            connector=MagicMock(),
            close_connection=AsyncMock(),
        )
    )

    async def _noop_async(_self: Any) -> None:
        return None

    with (
        patch.object(AsyncSandboxBatch, "_start", lambda _self: None),
        patch.object(AsyncSandboxBatch, "_create_worker", _noop_async),
    ):
        batch = await client.claim_batch(
            groups if groups is not None else [BatchGroup(warmpool="pool-a", size=1)],
            batch_id=batch_id,
            lease_duration=lease_duration,
        )
    batch._holder_identity = holder_identity
    assert batch._lease.spec is not None
    batch._lease.spec.holder_identity = holder_identity
    helper.read_batch_lease = AsyncMock(side_effect=lambda *_a, **_kw: batch._lease)
    helper.replace_batch_lease = AsyncMock(
        side_effect=lambda _n, _ns, body, **_kw: _make_async_lease(
            holder_identity=body.spec.holder_identity,
            resource_version="99",
            lease_duration_seconds=body.spec.lease_duration_seconds or lease_duration,
            batch_id=batch_id,
        )
    )
    return client, helper, batch


# ===========================================================================
# CELL 01: Watch 403 Forbidden vs Lease Renewal Authority Separation & HTTP Watch
# ===========================================================================


class TestCell01WatchVsLeaseAuthoritySeparation(unittest.TestCase):
    """Cell 01 (Negative Control / F-01: INV-AUTH-1 & INV-AUTH-2): Watch fatal errors
    must never halt Lease renewal or block clean member release while the Lease
    remains valid and connected sandboxes are active.
    """

    def test_watch_fatal_error_does_not_halt_lease_renewal(self) -> None:
        _, helper, batch = _new_stopped_sync_batch()
        watch_started = threading.Event()
        release_watch_403 = threading.Event()

        def watch_with_403(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
            watch_started.set()
            release_watch_403.wait(timeout=2.0)
            raise k8s_client.ApiException(status=403, reason="Forbidden")

        rv_counter = 10

        def replace_lease(
            _name: str, _ns: str, lease_body: Any, **_kwargs: Any
        ) -> k8s_client.V1Lease:
            nonlocal rv_counter
            rv_counter += 1
            return _make_lease(
                holder_identity=lease_body.spec.holder_identity,
                resource_version=str(rv_counter),
                lease_duration_seconds=lease_body.spec.lease_duration_seconds,
            )

        helper.watch_sandbox_claims.side_effect = watch_with_403
        helper.replace_batch_lease.side_effect = replace_lease

        try:
            batch._state.upsert_claim(_claim_obj("b1-0"))
            ready_members = batch.wait_for_quorum()
            self.assertEqual(len(ready_members), 1)
            active_sandbox = batch.connect(ready_members[0])
            self.assertIsNotNone(active_sandbox)

            setattr(batch, "_renew_interval", 0.03)
            batch._watch_thread = threading.Thread(target=batch._watch_loop, daemon=True)
            batch._renew_thread = threading.Thread(target=batch._renew_loop, daemon=True)
            batch._watch_thread.start()
            batch._renew_thread.start()

            self.assertTrue(watch_started.wait(timeout=1.0))
            release_watch_403.set()
            batch._watch_thread.join(timeout=1.0)
            self.assertFalse(batch._watch_thread.is_alive())

            time.sleep(0.10)
            self.assertTrue(
                batch._renew_once(),
                "INV-AUTH-1 violated: _renew_once() returned False after watch 403 even though "
                "Lease is valid and connected sandbox is active",
            )
            self.assertTrue(
                batch._renew_thread.is_alive(),
                "INV-AUTH-1 violated: _renew_thread exited after watch 403 while connected "
                "sandbox is active",
            )
            assert batch._lease.metadata is not None
            self.assertEqual(batch._lease.metadata.resource_version, str(rv_counter))
            self.assertFalse(batch.lease_degraded())

            batch.release_member(ready_members[0])
            helper.delete_sandbox_claim.assert_called_once()
        finally:
            batch._renew_stop.set()
            batch._watch_stop.set()
            for th in (batch._renew_thread, batch._watch_thread):
                if th is not None and th.is_alive():
                    th.join(timeout=1.0)


class TestCell01AsyncWatchAndHttpStatusHandling(unittest.IsolatedAsyncioTestCase):
    """Async counterpart for Cell 01 (F-01: INV-AUTH-1 & INV-AUTH-2, F-09: INV-WATCH-HTTP-1)."""

    async def test_async_watch_fatal_error_does_not_halt_lease_renewal(self) -> None:
        _, helper, batch = await _new_stopped_async_batch()

        async def watch_with_403(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
            raise async_k8s_client.ApiException(status=403, reason="Forbidden")
            yield {}  # pragma: no cover

        rv_counter = 10

        async def replace_lease(
            _name: str, _ns: str, lease_body: Any, **_kwargs: Any
        ) -> async_k8s_client.V1Lease:
            nonlocal rv_counter
            rv_counter += 1
            return _make_async_lease(
                holder_identity=lease_body.spec.holder_identity,
                resource_version=str(rv_counter),
                lease_duration_seconds=lease_body.spec.lease_duration_seconds,
            )

        helper.watch_sandbox_claims = watch_with_403
        helper.replace_batch_lease = AsyncMock(side_effect=replace_lease)

        try:
            batch._state.upsert_claim(_claim_obj("b1-0"))
            ready_members = await batch.wait_for_quorum()
            active_sandbox = await batch.connect(ready_members[0])
            self.assertIsNotNone(active_sandbox)

            setattr(batch, "_renew_interval", 0.03)
            batch._watch_task = asyncio.create_task(batch._watch_loop())
            batch._renew_task = asyncio.create_task(batch._renew_loop())

            await asyncio.wait_for(batch._watch_task, timeout=1.0)
            await asyncio.sleep(0.08)

            self.assertTrue(
                await batch._renew_once(),
                "INV-AUTH-1 violated: AsyncSandboxBatch._renew_once() returned False after "
                "watch 403",
            )
            self.assertFalse(batch._renew_task.done())
            await batch.release_member(ready_members[0])
        finally:
            for task in (batch._renew_task, batch._watch_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    async def test_async_watch_sandbox_claims_raises_api_exception_with_headers_on_http_error(
        self,
    ) -> None:
        """Reproduces F-09 (INV-WATCH-HTTP-1): AsyncK8sHelper.watch_sandbox_claims uses
        `watch.Watch().stream(_preload_content=False)`, which does NOT raise on non-2xx
        responses. Plain-text 403 responses are silently dropped as 0 events, and JSON 429
        Status responses lose HTTP `Retry-After` headers.
        """

        class _FakeContent:
            def __init__(self, lines: list[bytes]) -> None:
                self._lines = list(lines)

            async def readline(self) -> bytes:
                return self._lines.pop(0) if self._lines else b""

            async def read(self) -> bytes:
                data = b"".join(self._lines)
                self._lines.clear()
                return data

        class _FakeResponse:
            def __init__(
                self, status: int, reason: str, headers: dict[str, str], body: bytes
            ) -> None:
                self.status = status
                self.reason = reason
                self.headers = headers
                self._body = body
                self.content = _FakeContent([body])

            async def read(self) -> bytes:
                return self._body

            async def text(self) -> str:
                return self._body.decode("utf-8", errors="replace")

            def close(self) -> None:
                return None

            def release(self) -> None:
                return None

        helper = AsyncK8sHelper.__new__(AsyncK8sHelper)
        helper._initialized = True
        helper._init_lock = asyncio.Lock()
        custom_api = MagicMock()
        helper.custom_objects_api = custom_api

        # Case 1: Non-JSON HTTP 403 Forbidden from proxy/RBAC gate raises ApiException(status=403).
        custom_api.list_namespaced_custom_object = AsyncMock(
            return_value=_FakeResponse(403, "Forbidden", {}, b"Forbidden\n")
        )
        with self.assertRaises(
            async_k8s_client.ApiException,
            msg="F-09 (INV-WATCH-HTTP-1): plain-text HTTP 403 on watch_sandbox_claims was "
            "silently swallowed as an empty 0-event watch stream!",
        ) as ctx_403:
            _ = [
                ev
                async for ev in helper.watch_sandbox_claims(
                    "default", "agents.x-k8s.io/batch-id=b1", "5", 30
                )
            ]
        self.assertEqual(ctx_403.exception.status, 403)

        # Case 2: JSON HTTP 429 Status must preserve HTTP Retry-After header on ApiException.
        status_429 = (
            json.dumps({
                "kind": "Status",
                "apiVersion": "v1",
                "status": "Failure",
                "code": 429,
                "reason": "TooManyRequests",
            }).encode("utf-8")
            + b"\n"
        )
        custom_api.list_namespaced_custom_object = AsyncMock(
            return_value=_FakeResponse(429, "Too Many Requests", {"Retry-After": "17"}, status_429)
        )
        with self.assertRaises(async_k8s_client.ApiException) as ctx_429:
            _ = [
                ev
                async for ev in helper.watch_sandbox_claims(
                    "default", "agents.x-k8s.io/batch-id=b1", "5", 30
                )
            ]
        self.assertEqual(ctx_429.exception.status, 429)
        exc_headers: Any = ctx_429.exception.headers or {}
        self.assertEqual(exc_headers.get("Retry-After"), "17")


# ===========================================================================
# CELL 02: Watch 410 Gone, Tombstones, & Late Watch ADDED After Release (F-03)
# ===========================================================================


class TestCell02Watch410GoneAndLateAddedLeak(unittest.TestCase):
    """Cell 02 (INV-OWN-4 & F-03 INV-LEAK-1): 410 Gone preserves tombstones, and
    late watch ADDED events arriving after release_not_ready() / release_group()
    must delete the live SandboxClaim in Kubernetes rather than orphaning it.
    """

    def test_watch_410_relist_preserves_released_tombstones_and_marks_lost(self) -> None:
        _, helper, batch = _new_stopped_sync_batch([
            BatchGroup(warmpool="pool-a", size=3, min_ready=1)
        ])
        batch._state.upsert_claim(_claim_obj("b1-0", sandbox_name="sbx-0", group_size="3"))
        batch._state.upsert_claim(
            _claim_obj("b1-1", ready="False", reason="Starting", sandbox_name=None, group_size="3")
        )
        batch._state.upsert_claim(_claim_obj("b1-2", sandbox_name="sbx-2", group_size="3"))

        ev_iter = batch.events()
        first_ev = next(ev_iter)
        second_ev = next(ev_iter)
        self.assertEqual(
            {first_ev.member.claim_name, second_ev.member.claim_name}, {"b1-0", "b1-2"}
        )

        m0 = [m for m in batch.members() if m.claim_name == "b1-0"][0]
        batch._state.release_member(m0.claim_name)

        calls = 0

        def watch_stream(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise k8s_client.ApiException(status=410, reason="Gone")
            batch._watch_stop.set()
            return iter(())

        helper.watch_sandbox_claims.side_effect = watch_stream
        helper.list_sandbox_claim_objects.return_value = (
            [
                _claim_obj("b1-0", sandbox_name="sbx-0", rv="20", group_size="3"),
                _claim_obj("b1-1", sandbox_name="sbx-1", rv="20", group_size="3"),
            ],
            "20",
        )

        batch._watch_loop()

        by_name = {m.claim_name: m for m in batch.members()}
        self.assertNotIn("b1-0", by_name, "INV-OWN-4: released claim resurrected during 410 relist")
        self.assertEqual(by_name["b1-1"].state, MemberState.READY)
        self.assertEqual(by_name["b1-2"].state, MemberState.LOST)
        self.assertEqual(helper.watch_sandbox_claims.call_args_list[-1].args[2], "20")

    def test_late_watch_added_after_release_not_ready_deletes_claim_in_k8s(self) -> None:
        """Reproduces F-03 (INV-LEAK-1 / INV-OWN-4):
        1. `_create_worker` finishes creating `b1-0` and `b1-1` (`_creators` finished).
        2. Watch delivers `b1-0` Ready first; consumer reaches quorum (`min_ready=1`) and
           calls `batch.release_not_ready()` before watch delivers `ADDED(b1-1)`.
        3. Watch then delivers `ADDED(b1-1)` (`rv="12"`). Because `batch_state.upsert_claim`
           calls `release_claim("b1-1", ..., needs_delete=False)`, `b1-1` is tombstoned in
           `_released` with zero K8s DELETE calls, permanently leaking `b1-1` live in K8s!
        """
        _, helper, batch = _new_stopped_sync_batch([
            BatchGroup(warmpool="pool-a", size=2, min_ready=1)
        ])
        batch._state.upsert_claim(
            _claim_obj("b1-0", sandbox_name="sbx-0", group_size="2", min_ready="1")
        )
        ready = batch.wait_for_quorum()
        self.assertEqual([m.claim_name for m in ready], ["b1-0"])

        # Caller releases unready tail before watch delivers ADDED for already-created b1-1.
        batch.release_not_ready()
        helper.delete_sandbox_claim.assert_not_called()

        def watch_delivers_late_b1_1(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
            yield {
                "type": "ADDED",
                "object": _claim_obj(
                    "b1-1",
                    ready="False",
                    reason="Starting",
                    sandbox_name=None,
                    rv="12",
                    group_size="2",
                    min_ready="1",
                ),
            }
            batch._watch_stop.set()

        helper.watch_sandbox_claims.side_effect = watch_delivers_late_b1_1
        batch._watch_loop()
        batch._wait_active_deletes()

        # Either watch_loop drains pending_deletes immediately or queues b1-1 in _pending_deletes
        # and deletes it; zero DELETEs with empty _pending_deletes leaks b1-1 until shutdownTime!
        deleted_names = [c.args[0] for c in helper.delete_sandbox_claim.call_args_list]
        self.assertIn(
            "b1-1",
            deleted_names,
            "F-03 (INV-LEAK-1) violated: late watch ADDED('b1-1') after release_not_ready() "
            f"tombstoned 'b1-1' in _released={batch._state._released} with _pending_deletes="
            f"{batch._state._pending_deletes} and sent 0 delete_sandbox_claim calls!",
        )


# ===========================================================================
# CELL 03: Watch TLS SSLEOFError (Transient) vs SSLCertVerificationError (Fatal)
# ===========================================================================


class TestCell03WatchTlsTransientVsFatal(unittest.TestCase):
    """Cell 03: SSLEOFError retries watch stream; SSLCertVerificationError halts watch."""

    def test_watch_ssl_eof_retries_while_cert_verification_error_stops(self) -> None:
        _, helper, batch = _new_stopped_sync_batch()
        calls = 0

        def watch_eof_then_ready(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
            yield {"type": "ADDED", "object": _claim_obj("b1-0", rv="9")}
            batch._watch_stop.set()

        helper.watch_sandbox_claims.side_effect = watch_eof_then_ready
        with patch.object(batch._watch_stop, "wait", return_value=False):
            batch._watch_loop()
        self.assertEqual(calls, 2)
        self.assertIsNone(batch.err())
        self.assertEqual(batch.members()[0].state, MemberState.READY)

        _, helper2, batch2 = _new_stopped_sync_batch(batch_id="b2")
        cert_err = ssl.SSLCertVerificationError(1, "certificate verify failed")
        helper2.watch_sandbox_claims.side_effect = cert_err
        batch2._watch_loop()
        self.assertEqual(helper2.watch_sandbox_claims.call_count, 1)
        self.assertIs(batch2.err(), cert_err)


# ===========================================================================
# CELL 04: Watch Short-Stream Exponential Backoff & Healthy Reset
# ===========================================================================


class TestCell04WatchShortStreamBackoff(unittest.TestCase):
    """Cell 04: Short watch disconnects apply exponential backoff; long watch resets failures."""

    def test_short_watch_increments_backoff_and_long_watch_resets_counter(self) -> None:
        _, helper, batch = _new_stopped_sync_batch()
        waits: list[float] = []

        monotonic_vals = iter([
            100.0,
            100.1,
            101.0,
            101.1,
            103.0,
            168.0,
            168.0,
            168.1,
        ])

        def fake_watch(*_args: Any, **_kwargs: Any) -> Iterator[dict[str, Any]]:
            return iter(())

        def fake_wait(delay: float) -> bool:
            waits.append(delay)
            if len(waits) >= 3:
                batch._watch_stop.set()
                return True
            return False

        helper.watch_sandbox_claims.side_effect = fake_watch
        with (
            patch(
                "k8s_agent_sandbox.sandbox_batch.time.monotonic",
                side_effect=lambda: next(monotonic_vals),
            ),
            patch(
                "k8s_agent_sandbox.sandbox_batch.batch_utils.retry_delay",
                side_effect=lambda failures, _e: float(failures),
            ),
            patch.object(batch._watch_stop, "wait", side_effect=fake_wait),
        ):
            batch._watch_loop()

        self.assertEqual(waits, [1.0, 2.0, 1.0])


# ===========================================================================
# CELL 05: Renewal 409 Conflict Same-Holder Recovery vs Foreign Takeover
# ===========================================================================


class TestCell05Renewal409ConflictSameVsForeignHolder(unittest.TestCase):
    """Cell 05 (INV-OWN-1): 409 with same holderIdentity recovers; foreign holder fences."""

    def test_renewal_409_recovers_when_same_holder_and_fences_when_taken_over(self) -> None:
        _, helper, batch = _new_stopped_sync_batch(holder_identity="h1")
        conflict = k8s_client.ApiException(status=409, reason="Conflict")

        helper.replace_batch_lease.side_effect = [
            conflict,
            _make_lease(holder_identity="h1", resource_version="12"),
        ]
        helper.read_batch_lease.side_effect = None
        helper.read_batch_lease.return_value = _make_lease(
            holder_identity="h1", resource_version="11"
        )
        self.assertTrue(batch._renew_once())
        assert batch._lease.metadata is not None
        self.assertEqual(batch._lease.metadata.resource_version, "12")
        self.assertFalse(batch.lease_degraded())
        self.assertIsNone(batch.err())

        helper.replace_batch_lease.side_effect = conflict
        helper.read_batch_lease.return_value = _make_lease(
            holder_identity="h2", resource_version="13"
        )
        self.assertFalse(batch._renew_once())
        self.assertTrue(batch.lease_degraded())
        self.assertIsInstance(batch.err(), BatchInUseError)


# ===========================================================================
# CELL 06: Renewal 503 Degraded Flag, Recovery, and Expiry Self-Fencing
# ===========================================================================


class TestCell06Renewal503DegradedRecoveryAndExpiryFence(unittest.TestCase):
    """Cell 06 (INV-AUTH-3): Transient 503 marks lease_degraded() True, clears on
    success, and self-fences with BatchLeaseExpiredError once lease_expires_at passes.
    """

    def test_renewal_503_transitions_degraded_recovers_and_fences_on_expiry(self) -> None:
        _, helper, batch = _new_stopped_sync_batch(lease_duration=60, holder_identity="h1")

        helper.replace_batch_lease.side_effect = k8s_client.ApiException(
            status=503, reason="Unavailable"
        )
        self.assertTrue(batch._renew_once())
        self.assertTrue(batch.lease_degraded())
        self.assertIsNone(batch.err())

        helper.replace_batch_lease.side_effect = None
        helper.replace_batch_lease.return_value = _make_lease(
            holder_identity="h1", resource_version="12"
        )
        self.assertTrue(batch._renew_once())
        self.assertFalse(batch.lease_degraded())

        batch._lease_expires_at = time.monotonic() - 1.0
        batch._renew_loop()
        self.assertTrue(batch.lease_degraded())
        self.assertIsInstance(batch.err(), BatchLeaseExpiredError)


# ===========================================================================
# CELL 07: Renewal 404 External Lease Deletion & TLS Cert Error
# ===========================================================================


class TestCell07Renewal404AndTlsCertFailure(unittest.TestCase):
    """Cell 07: External Lease 404 fences immediately; TLS cert error marks degraded."""

    def test_renewal_404_stops_immediately_with_lease_expired_error(self) -> None:
        _, helper, batch = _new_stopped_sync_batch(holder_identity="h1")
        helper.replace_batch_lease.side_effect = k8s_client.ApiException(
            status=404, reason="Not Found"
        )
        self.assertFalse(batch._renew_once())
        self.assertTrue(batch.lease_degraded())
        self.assertIsInstance(batch.err(), BatchLeaseExpiredError)

    def test_renewal_ssl_cert_error_marks_lease_degraded(self) -> None:
        _, helper, batch = _new_stopped_sync_batch(holder_identity="h1")
        helper.replace_batch_lease.side_effect = ssl.SSLCertVerificationError(1, "cert expired")
        self.assertTrue(batch._renew_once())
        self.assertTrue(batch.lease_degraded())


# ===========================================================================
# CELL 08: Create 429 Too Many Requests Cooldown & Warm-Credit Pause
# ===========================================================================


class TestCell08Create429CooldownAndWarmCreditPause(unittest.TestCase):
    """Cell 08: HTTP 429 Retry-After sets WarmCredit throttle and pauses warm credit bypass."""

    def test_create_429_sets_global_cooldown_and_marks_fill_claim_create_failed(self) -> None:
        _, helper, batch = _new_stopped_sync_batch([BatchGroup(warmpool="pool-a", size=1)])
        api_429 = k8s_client.ApiException(status=429, reason="Too Many Requests")
        api_429.headers = {"Retry-After": "12"}
        helper.create_sandbox_claim.side_effect = [api_429, api_429, api_429]

        before = time.monotonic()
        with patch.object(batch._stop_creating, "wait", return_value=False):
            batch._create_worker()

        self.assertGreaterEqual(batch._warm_credit._throttled_until, before + 11.5)
        by_name = {m.claim_name: m for m in batch.members()}
        self.assertEqual(by_name["b1-0"].state, MemberState.FAILED)
        self.assertEqual(by_name["b1-0"].reason, "CreateFailed")

        batch._warm_credit._credit["pool-a"] = 5
        self.assertFalse(batch._warm_credit.take("pool-a"))

    def test_release_group_cancels_popped_pacing_slot_workers_without_stalling_events(
        self,
    ) -> None:
        """Reproduces F-25 (INV-ACCT-2): With default `max_in_flight=20`, `_create_worker`
        threads pop `(claim_name, group)` from `self._create_plan` (`sandbox_batch.py:1787`)
        BEFORE sleeping on staggered `create_rps` slots (`:1800`). Calling `release_group()`
        runs `_drop_planned_creates()` against an already-empty `_create_plan`, failing to
        mark popped unstarted claims as unable or wake the sleeping workers, stalling
        `events()` / `group_outcomes()` / `wait_for_quorum()` across the full pacing schedule!
        """
        _, helper, batch = _new_stopped_sync_batch([
            BatchGroup(warmpool="pool-fast", size=1),
            BatchGroup(warmpool="pool-slow", size=3),
        ])
        batch._warm_credit._credit["pool-fast"] = 1
        batch._warm_credit._credit["pool-slow"] = 0
        batch._create_interval = 5.0
        batch._next_create_at = time.monotonic() + batch._create_interval

        def create_and_upsert(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            obj = _claim_obj(claim_name, warmpool=warmpool, sandbox_name=f"sbx-{claim_name}")
            with batch._changed:
                batch._state.upsert_claim(obj)
                batch._changed.notify_all()
            return obj

        helper.create_sandbox_claim.side_effect = create_and_upsert
        workers = [threading.Thread(target=batch._create_worker, daemon=True) for _ in range(4)]
        batch._creators = workers
        for w in workers:
            w.start()

        try:
            ev_iter = batch.events()
            first_ev = next(ev_iter)
            self.assertEqual(first_ev.member.warmpool, "pool-fast")

            # Wait briefly so workers 1..3 have popped b1-1..b1-3 into _stop_creating.wait.
            deadline = time.monotonic() + 1.0
            while batch._create_plan and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(len(batch._create_plan), 0)

            batch.release_group("pool-slow")

            drained: list[Any] = []
            drain_done = threading.Event()

            def drain_remaining() -> None:
                drained.extend(ev_iter)
                drain_done.set()

            drainer = threading.Thread(target=drain_remaining, daemon=True)
            drainer.start()
            finished_promptly = drain_done.wait(timeout=0.35)
            self.assertTrue(
                finished_promptly,
                "F-25 (INV-ACCT-2) violated: release_group('pool-slow') failed to resolve or "
                "wake popped pacing-slot workers, stalling batch.events() on 5s..15s sleeps!",
            )
            self.assertEqual(drained, [])
        finally:
            batch._stop_creating.set()
            with batch._changed:
                batch._state.note_error(BatchError("cleanup"))
                batch._changed.notify_all()
            for w in workers:
                w.join(timeout=1.0)


class TestCell08AsyncCreatePacingSlotCancellation(unittest.IsolatedAsyncioTestCase):
    """Cell 08 (Async): F-25 popped pacing-slot worker cancellation on release_group."""

    async def test_async_release_group_cancels_popped_pacing_slot_workers_without_stalling_events(
        self,
    ) -> None:
        """Reproduces F-25 (INV-ACCT-2) on AsyncSandboxBatch: `_create_worker` tasks pop
        `(claim_name, group)` from `self._create_plan` (`async_sandbox_batch.py:1776`) BEFORE
        sleeping on staggered `create_rps` slots (`:1782`). Calling `await batch.release_group()`
        fails to resolve or wake popped pacing-slot tasks, stalling `batch.events()`!
        """
        _, helper, batch = await _new_stopped_async_batch([
            BatchGroup(warmpool="pool-fast", size=1),
            BatchGroup(warmpool="pool-slow", size=3),
        ])
        batch._warm_credit._credit["pool-fast"] = 1
        batch._warm_credit._credit["pool-slow"] = 0
        batch._create_interval = 5.0
        batch._next_create_at = time.monotonic() + batch._create_interval

        async def create_and_upsert(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            obj = _claim_obj(claim_name, warmpool=warmpool, sandbox_name=f"sbx-{claim_name}")
            async with batch._changed:
                batch._state.upsert_claim(obj)
                batch._changed.notify_all()
            return obj

        helper.create_sandbox_claim = AsyncMock(side_effect=create_and_upsert)
        workers = [asyncio.create_task(batch._create_worker()) for _ in range(4)]
        batch._creators = set(workers)

        try:
            ev_iter = batch.events()
            first_ev = await asyncio.wait_for(anext(ev_iter), timeout=1.0)
            self.assertEqual(first_ev.member.warmpool, "pool-fast")

            deadline = time.monotonic() + 1.0
            while batch._create_plan and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            self.assertEqual(len(batch._create_plan), 0)

            await batch.release_group("pool-slow")

            async def drain_remaining() -> list[Any]:
                return [ev async for ev in ev_iter]

            try:
                drained = await asyncio.wait_for(drain_remaining(), timeout=0.35)
            except TimeoutError:
                self.fail(
                    "F-25 (INV-ACCT-2) violated: AsyncSandboxBatch.release_group('pool-slow') "
                    "failed to resolve or wake popped pacing-slot tasks, stalling batch.events()!"
                )
            self.assertEqual(drained, [])
        finally:
            batch._stop_creating.set()
            async with batch._changed:
                batch._state.note_error(BatchError("cleanup"))
                batch._changed.notify_all()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)


# ===========================================================================
# CELL 09: Acquire Warm-Pool Precheck 404 Eviction & Subsequent Recovery
# ===========================================================================


class TestCell09AcquireWarmPool404PrecheckEviction(unittest.TestCase):
    """Cell 09: WarmPool 404 raises SandboxWarmPoolNotFoundError and clears _prechecks."""

    def test_acquire_warmpool_404_evicts_precheck_allowing_later_retry(self) -> None:
        _, helper, batch = _new_stopped_sync_batch([BatchGroup(warmpool="pool-a", size=0)])
        helper.get_sandbox_warmpool.return_value = None

        with self.assertRaises(SandboxWarmPoolNotFoundError):
            batch.acquire("pool-new", timeout=1.0)
        self.assertNotIn("pool-new", batch._prechecks)

        helper.get_sandbox_warmpool.return_value = {
            "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
            "status": {"readyReplicas": 1},
        }
        helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}

        def create_and_mark_ready(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            obj = _claim_obj(claim_name, warmpool=warmpool, sandbox_name="sbx-dyn")
            with batch._changed:
                batch._state.upsert_claim(obj)
                batch._wake_acquire(claim_name)
                batch._changed.notify_all()
            return obj

        helper.create_sandbox_claim.side_effect = create_and_mark_ready
        member = batch.acquire("pool-new", timeout=2.0)
        self.assertEqual(member.warmpool, "pool-new")
        self.assertEqual(member.state, MemberState.READY)


# ===========================================================================
# CELL 10: Acquire Missing Template & Terminal Claim Failure Cleanup
# ===========================================================================


class TestCell10AcquireTemplateMissingAndTerminalMemberError(unittest.TestCase):
    """Cell 10: Missing template raises SandboxTemplateNotFoundError; terminal Ready=False
    raises TerminalMemberError and deletes the failed claim.
    """

    def test_acquire_missing_template_and_terminal_claim_failure(self) -> None:
        _, helper, batch = _new_stopped_sync_batch([BatchGroup(warmpool="pool-a", size=0)])
        helper.get_sandbox_warmpool.return_value = {
            "spec": {"sandboxTemplateRef": {"name": "missing-tmpl"}},
            "status": {"readyReplicas": 0},
        }
        helper.get_sandbox_template.return_value = None

        with self.assertRaises(SandboxTemplateNotFoundError):
            batch.acquire("pool-bad-tmpl", timeout=1.0)
        self.assertNotIn("pool-bad-tmpl", batch._prechecks)

        helper.get_sandbox_template.return_value = {"metadata": {"name": "missing-tmpl"}}

        def create_terminal_claim(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            obj = _claim_obj(
                claim_name,
                warmpool=warmpool,
                ready="False",
                reason="InvalidConfiguration",
                sandbox_name=None,
            )
            with batch._changed:
                batch._state.upsert_claim(obj)
                batch._wake_acquire(claim_name)
                batch._changed.notify_all()
            return obj

        helper.create_sandbox_claim.side_effect = create_terminal_claim
        with self.assertRaises(TerminalMemberError):
            batch.acquire("pool-bad-tmpl", timeout=2.0)
        helper.delete_sandbox_claim.assert_called_once()


# ===========================================================================
# CELL 11: SDK Acquire Cancellation & CANCEL-5 Defense-in-Depth Hardening
# ===========================================================================


class TestCell11SdkAcquireCancellation(unittest.IsolatedAsyncioTestCase):
    """Cell 11: SDK AsyncSandboxBatch rolls back cancelled acquire() cleanly under
    concurrent watch activity, and wakes acquire() retry backoff on release_group (F-26).
    """

    async def test_sdk_async_acquire_cancellation_rolls_back_and_deletes_claim(self) -> None:
        _, helper, batch = await _new_stopped_async_batch([BatchGroup(warmpool="pool-a", size=0)])
        created_event = asyncio.Event()

        async def create_not_ready(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            obj = _claim_obj(
                claim_name, warmpool=warmpool, ready="False", reason="Starting", sandbox_name=None
            )
            async with batch._changed:
                batch._state.upsert_claim(obj)
                batch._changed.notify_all()
            created_event.set()
            return obj

        helper.create_sandbox_claim = AsyncMock(side_effect=create_not_ready)
        acq_task = asyncio.create_task(batch.acquire("pool-a", timeout=10.0))
        await asyncio.wait_for(created_event.wait(), timeout=1.0)
        await asyncio.sleep(0.02)

        acq_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await acq_task

        await batch._wait_active_deletes()
        self.assertEqual(batch._state._checking_acquires.get("pool-a", 0), 0)
        helper.delete_sandbox_claim.assert_awaited_once()

    async def test_sdk_async_acquire_repeated_cancellation_with_concurrent_watch(
        self,
    ) -> None:
        """Verifies CANCEL-5 rollback under genuine concurrent watch notifications and
        repeated task.cancel() calls without synthetic external _changed acquisition.
        """
        _, helper, batch = await _new_stopped_async_batch([BatchGroup(warmpool="pool-a", size=0)])
        batch._warm_credit._credit["pool-a"] = 8
        created_names: list[str] = []
        all_created = asyncio.Event()

        async def create_not_ready(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            obj = _claim_obj(
                claim_name, warmpool=warmpool, ready="False", reason="Starting", sandbox_name=None
            )
            async with batch._changed:
                batch._state.upsert_claim(obj)
                batch._changed.notify_all()
            created_names.append(claim_name)
            if len(created_names) >= 4:
                all_created.set()
            return obj

        helper.create_sandbox_claim = AsyncMock(side_effect=create_not_ready)
        tasks = [asyncio.create_task(batch.acquire("pool-a", timeout=10.0)) for _ in range(4)]
        await asyncio.wait_for(all_created.wait(), timeout=1.0)
        await asyncio.sleep(0.01)

        async def noisy_watch_updates() -> None:
            for step in range(8):
                async with batch._changed:
                    for name in list(created_names):
                        batch._state.upsert_claim(
                            _claim_obj(
                                name,
                                warmpool="pool-a",
                                ready="False",
                                reason="Starting",
                                sandbox_name=None,
                                rv=str(20 + step),
                            )
                        )
                    batch._changed.notify_all()
                await asyncio.sleep(0)

        watch_driver = asyncio.create_task(noisy_watch_updates())
        for t in tasks:
            t.cancel()
        await asyncio.sleep(0)
        for t in tasks:
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t
        await watch_driver
        await batch._wait_active_deletes()

        self.assertEqual(len(batch._acquire_waiters), 0)
        self.assertEqual(batch._state._checking_acquires.get("pool-a", 0), 0)
        for name in created_names:
            self.assertIn(name, batch._state._released)
        self.assertEqual(helper.delete_sandbox_claim.await_count, 4)

    async def test_sdk_async_acquire_retry_backoff_wakes_immediately_on_release_group(
        self,
    ) -> None:
        """Reproduces F-26: When `acquire()` hits a retryable `429` with `Retry-After: 15`,
        `_create_one` sleeps on `self._stop_creating.wait()` (`async_sandbox_batch.py:2039`)
        instead of the acquire's `waiter`. Calling `release_group('pool-a')` sets `waiter`
        without setting `_stop_creating`, stranding `acquire()` for the full 15s Retry-After!
        """
        _, helper, batch = await _new_stopped_async_batch([BatchGroup(warmpool="pool-a", size=0)])
        entered_429 = asyncio.Event()
        api_429 = async_k8s_client.ApiException(status=429, reason="Too Many Requests")
        setattr(api_429, "headers", {"Retry-After": "15"})

        async def fail_first_with_429(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            entered_429.set()
            raise api_429

        helper.create_sandbox_claim = AsyncMock(side_effect=fail_first_with_429)
        acq_task = asyncio.create_task(batch.acquire("pool-a", timeout=30.0))
        try:
            await asyncio.wait_for(entered_429.wait(), timeout=1.0)
            await asyncio.sleep(0.03)
            await batch.release_group("pool-a")
            with self.assertRaises(BatchError):
                await asyncio.wait_for(acq_task, timeout=0.35)
        except TimeoutError:
            self.fail(
                "F-26 violated: AsyncSandboxBatch.acquire() remained asleep in 15s Retry-After "
                "backoff after release_group('pool-a') instead of waking immediately!"
            )
        finally:
            batch._stop_creating.set()
            if not acq_task.done():
                acq_task.cancel()
                try:
                    await acq_task
                except (asyncio.CancelledError, BaseException):
                    pass


# ===========================================================================
# CELL 12: Teardown, Lease-Clamped Group Delete, Client Close, Connect, & Reaper
# ===========================================================================


class TestCell12SdkTeardownFencingAndReaper(unittest.TestCase):
    """Cell 12 (Sync): Connection retry, F-04 release_group Lease deadline clamping,
    F-16 _watch_thread stop/join, F-17 connect closed-cache & terminate Lease fence,
    F-18 batch_reaper 410 pagination & malformed renew_time isolation,
    F-24 release() vs in-flight acquire() create barrier, and F-27 get_batch fencing.
    """

    def test_sdk_failed_close_connection_during_release_retries_on_client_cleanup(self) -> None:
        client, _helper, batch = _new_stopped_sync_batch()
        batch._state.upsert_claim(_claim_obj("b1-0"))
        ready = batch.wait_for_quorum()
        sbx: Any = batch.connect(ready[0])

        sbx.close_connection.side_effect = [RuntimeError("tunnel busy"), None]
        batch.release()
        self.assertIn(batch, client._closing_batches)

        client.delete_all()
        self.assertNotIn(batch, client._closing_batches)
        self.assertEqual(sbx.close_connection.call_count, 2)

    def test_release_group_clamps_delete_timeout_to_remaining_lease_window(self) -> None:
        """Reproduces F-04 (INV-OWN-3): `release_group()` calls `_delete_claims_in_rounds`
        with unclamped `_request_timeout=90` and `deadline=None` even when `_lease_expires_at`
        is only 2.5 seconds away, sending label-wide `deletecollection` that can outlive the
        Lease and race a successor `get_batch()` owner.
        """
        _client, helper, batch = _new_stopped_sync_batch(lease_duration=60)
        batch._state.upsert_claim(_claim_obj("b1-0", warmpool="pool-a"))
        batch._lease_expires_at = time.monotonic() + 2.5

        batch.release_group("pool-a")

        helper.delete_sandbox_claims_by_label.assert_called_once()
        del_timeout = helper.delete_sandbox_claims_by_label.call_args.kwargs.get("_request_timeout")
        self.assertIsNotNone(del_timeout)
        self.assertLessEqual(
            float(del_timeout),
            3.0,
            "F-04 (INV-OWN-3) violated: release_group() sent delete_sandbox_claims_by_label "
            f"with _request_timeout={del_timeout}s (> remaining 2.5s Lease window)!",
        )

    def test_sync_stop_background_threads_interrupts_and_joins_watch_thread(self) -> None:
        """Reproduces F-16 (INV-LEAK-2): `_stop_background_threads()` joins `_renew_thread`
        and `_creators` while setting `_watch_stop`, yet never calls `Watch.stop()` or joins
        `_watch_thread`, leaving `_watch_thread` blocked in `resp.stream()` after `release()`.
        """
        _client, helper, batch = _new_stopped_sync_batch()
        entered_watch = threading.Event()
        unblock_watch = threading.Event()

        class _FakeSock:
            def shutdown(self, _how: int) -> None:
                unblock_watch.set()

        class _FakeConn:
            def __init__(self) -> None:
                self.sock = _FakeSock()

        class _FakeStreamingResp:
            def __init__(self) -> None:
                self.status = 200
                self.connection = _FakeConn()

            def stream(self, **_kwargs: Any) -> Iterator[bytes]:
                entered_watch.set()
                unblock_watch.wait(timeout=5.0)
                return iter(())

            def close(self) -> None:
                return None

            def release_conn(self) -> None:
                return None

        real_watch_helper = K8sHelper.__new__(K8sHelper)
        real_watch_helper._batch_custom_objects_api = MagicMock()
        real_watch_helper._batch_custom_objects_api.list_namespaced_custom_object.side_effect = (
            lambda **_kw: _FakeStreamingResp()
        )
        helper.watch_sandbox_claims.side_effect = lambda *a, **kw: K8sHelper.watch_sandbox_claims(
            real_watch_helper, *a, **kw
        )

        batch._watch_thread = threading.Thread(target=batch._watch_loop, daemon=True)
        batch._watch_thread.start()
        self.assertTrue(entered_watch.wait(timeout=1.0))

        try:
            t0 = time.monotonic()
            batch.release()
            elapsed = time.monotonic() - t0
            self.assertFalse(
                batch._watch_thread.is_alive(),
                "F-16 (INV-LEAK-2) violated: batch.release() returned while batch._watch_thread "
                "was still alive!",
            )
            self.assertLess(
                elapsed,
                1.0,
                "F-16 (INV-LEAK-2) violated: batch.release() waited for watch stream timeout "
                f"({elapsed:.2f}s) instead of interrupting Watch via Watch.stop()!",
            )
        finally:
            unblock_watch.set()
            batch._watch_thread.join(timeout=1.0)

    def test_release_drains_in_flight_acquire_create_without_leaking_claim(self) -> None:
        """Reproduces F-24 (INV-LEAK-1 / INV-OWN-3): `acquire()` runs `create_sandbox_claim`
        on the caller's thread outside `self._creators` and `_active_deletes`. When `release()`
        completes its label-wide delete and sets `self._detached = True` while `acquire()` is
        mid-POST, the POST lands on the cluster and `_drop_acquired -> _delete_claim` catches
        `BatchError` on `self._detached`, permanently orphaning the new SandboxClaim!
        """
        _client, helper, batch = _new_stopped_sync_batch([BatchGroup(warmpool="pool-a", size=0)])
        entered_post = threading.Event()
        allow_post_return = threading.Event()
        live_lock = threading.Lock()
        live_claims: set[str] = set()

        def slow_create(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            entered_post.set()
            allow_post_return.wait(timeout=2.0)
            with live_lock:
                live_claims.add(claim_name)
            return _claim_obj(
                claim_name, warmpool=warmpool, ready="False", reason="Starting", sandbox_name=None
            )

        def delete_single(claim_name: str, *_args: Any, **_kwargs: Any) -> bool:
            with live_lock:
                live_claims.discard(claim_name)
            return True

        def delete_by_label(*_args: Any, **_kwargs: Any) -> None:
            with live_lock:
                live_claims.clear()

        helper.create_sandbox_claim.side_effect = slow_create
        helper.delete_sandbox_claim.side_effect = delete_single
        helper.delete_sandbox_claims_by_label.side_effect = delete_by_label

        acq_errors: list[BaseException] = []

        def run_acquire() -> None:
            try:
                batch.acquire("pool-a", timeout=5.0)
            except BaseException as exc:
                acq_errors.append(exc)

        acq_thread = threading.Thread(target=run_acquire, daemon=True)
        rel_thread = threading.Thread(target=batch.release, daemon=True)
        try:
            acq_thread.start()
            self.assertTrue(entered_post.wait(timeout=1.0))
            rel_thread.start()
            # Give release() a chance to either wait on in-flight acquire or race past it.
            time.sleep(0.05)
            allow_post_return.set()
            acq_thread.join(timeout=2.0)
            rel_thread.join(timeout=2.0)
            batch._wait_active_deletes()
        finally:
            allow_post_return.set()
            acq_thread.join(timeout=1.0)
            rel_thread.join(timeout=1.0)

        self.assertEqual(len(acq_errors), 1)
        self.assertEqual(
            live_claims,
            set(),
            f"F-24 (INV-LEAK-1) violated: in-flight acquire() POST escaped release() barrier "
            f"and leaked live SandboxClaim(s) {live_claims} after batch.release() finished!",
        )

    def test_get_batch_fences_slow_attach_lease_write(self) -> None:
        """Reproduces F-27a (INV-AUTH-3): `get_batch` sends `replace_batch_lease(...,
        _request_timeout=30)` unclamped by `lease_duration=6` and returns an already-expired
        handle when the write takes 7s instead of raising `BatchLeaseExpiredError`.
        """
        with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
            norm_client: SandboxClient[Any] = SandboxClient(cleanup=False)
        norm_helper: Any = norm_client.k8s_helper
        norm_helper.api_pool_size.return_value = 25
        slow_lease = _make_lease(
            holder_identity="",
            resource_version="10",
            lease_duration_seconds=6,
            batch_id="b1",
        )
        assert slow_lease.metadata is not None
        slow_lease.metadata.annotations = {BATCH_LEASE_DURATION_ANNOTATION: "6"}
        norm_helper.read_batch_lease.return_value = slow_lease
        norm_helper.list_sandbox_claim_objects.return_value = ([], "5")
        fake_clock = [1000.0]

        def slow_replace(_n: str, _ns: str, body: Any, **_kw: Any) -> k8s_client.V1Lease:
            fake_clock[0] += 7.0
            return _make_lease(
                holder_identity=body.spec.holder_identity,
                resource_version="11",
                lease_duration_seconds=6,
            )

        norm_helper.replace_batch_lease.side_effect = slow_replace
        with (
            patch(
                "k8s_agent_sandbox.sandbox_batch.time.monotonic",
                side_effect=lambda: fake_clock[0],
            ),
            patch.object(SandboxBatch, "_start", lambda _s: None),
            self.assertRaises(
                BatchLeaseExpiredError,
                msg="F-27a (INV-AUTH-3) violated: get_batch() returned an already-expired handle "
                "after replace_batch_lease took 7s on a 6s Lease!",
            ),
        ):
            norm_client.get_batch("b1")

    def test_get_batch_validates_and_clamps_pool_size(self) -> None:
        """Reproduces F-27b: `get_batch` omits `batch_utils.check_pool_size`, leaving
        `_max_in_flight=20` on small `pool_size` clients and failing to reject `pool_size=2`.
        """
        with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
            small_client: SandboxClient[Any] = SandboxClient(cleanup=False)
        small_helper: Any = small_client.k8s_helper
        small_helper.api_pool_size.return_value = 2
        detached_lease = _make_lease(
            holder_identity="",
            resource_version="10",
            lease_duration_seconds=6,
            batch_id="b1",
        )
        assert detached_lease.metadata is not None
        detached_lease.metadata.annotations = {BATCH_LEASE_DURATION_ANNOTATION: "6"}
        small_helper.read_batch_lease.return_value = detached_lease
        small_helper.list_sandbox_claim_objects.return_value = ([], "5")
        small_helper.replace_batch_lease.return_value = _make_lease(
            holder_identity="new-h", resource_version="11", lease_duration_seconds=6
        )
        with (
            patch.object(SandboxBatch, "_start", lambda _s: None),
            self.assertRaises(
                ValueError,
                msg="F-27b violated: get_batch() skipped batch_utils.check_pool_size() and "
                "allowed pool_size=2 with _max_in_flight=20 (starving Lease renewal)!",
            ),
        ):
            small_client.get_batch("b1")

    def test_connect_recreates_closed_cached_sandbox(self) -> None:
        """Reproduces F-17a (INV-CONN-1): If a user calls `sbx.close_connection()`,
        `sbx.is_active` becomes `False`, yet `batch.connect(member)` returns the dead
        cached `Sandbox` handle!
        """
        with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
            client: SandboxClient[Any] = SandboxClient(
                connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
                cleanup=False,
            )
        helper: Any = client.k8s_helper
        helper.api_pool_size.return_value = 25
        helper.count_live_sandbox_claims.return_value = 0
        helper.get_sandbox_warmpool.return_value = {
            "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
            "status": {"readyReplicas": 2},
        }
        helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
        helper.create_batch_lease.return_value = _make_lease(holder_identity="h1")
        helper.list_sandbox_claim_objects.return_value = ([], "5")

        with (
            patch.object(SandboxBatch, "_start", lambda _s: None),
            patch.object(SandboxBatch, "_create_worker", lambda _s: None),
        ):
            batch = client.claim_batch([BatchGroup(warmpool="pool-a", size=1)], batch_id="b1")
        batch._holder_identity = "h1"
        assert batch._lease.spec is not None
        batch._lease.spec.holder_identity = "h1"
        helper.read_batch_lease.side_effect = lambda *_a, **_kw: batch._lease
        helper.replace_batch_lease.side_effect = lambda _n, _ns, body, **_kw: _make_lease(
            holder_identity=body.spec.holder_identity,
            resource_version="20",
        )

        batch._state.upsert_claim(_claim_obj("b1-0"))
        member = batch.wait_for_quorum()[0]
        sbx1 = batch.connect(member)
        self.assertTrue(sbx1.is_active)

        sbx1.close_connection()
        self.assertFalse(sbx1.is_active)

        sbx2 = batch.connect(member)
        self.assertTrue(
            sbx2.is_active,
            "F-17a (INV-CONN-1) violated: batch.connect(member) returned a closed cached Sandbox "
            "with is_active=False!",
        )

    def test_batch_connected_sandbox_terminate_respects_lease_fence(self) -> None:
        """Reproduces F-17b (INV-OWN-1 / INV-OWN-3): Calling `sbx.terminate()` on a
        batch-connected `Sandbox` invokes `delete_sandbox_claim` directly, bypassing
        `batch.release_member` and Lease fencing even after `batch.detach()`!
        """
        with patch("k8s_agent_sandbox.sandbox_client.K8sHelper"):
            client: SandboxClient[Any] = SandboxClient(
                connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
                cleanup=False,
            )
        helper: Any = client.k8s_helper
        helper.api_pool_size.return_value = 25
        helper.count_live_sandbox_claims.return_value = 0
        helper.get_sandbox_warmpool.return_value = {
            "spec": {"sandboxTemplateRef": {"name": "tmpl"}},
            "status": {"readyReplicas": 2},
        }
        helper.get_sandbox_template.return_value = {"metadata": {"name": "tmpl"}}
        helper.create_batch_lease.return_value = _make_lease(holder_identity="h1")
        helper.list_sandbox_claim_objects.return_value = ([], "5")

        with (
            patch.object(SandboxBatch, "_start", lambda _s: None),
            patch.object(SandboxBatch, "_create_worker", lambda _s: None),
        ):
            batch = client.claim_batch([BatchGroup(warmpool="pool-a", size=1)], batch_id="b1")
        batch._holder_identity = "h1"
        assert batch._lease.spec is not None
        batch._lease.spec.holder_identity = "h1"
        helper.read_batch_lease.side_effect = lambda *_a, **_kw: batch._lease
        helper.replace_batch_lease.side_effect = lambda _n, _ns, body, **_kw: _make_lease(
            holder_identity=body.spec.holder_identity,
            resource_version="20",
        )

        batch._state.upsert_claim(_claim_obj("b1-0"))
        member = batch.wait_for_quorum()[0]
        sbx = batch.connect(member)

        batch.detach(grace=30)
        with self.assertRaises(
            BatchError,
            msg="F-17b (INV-OWN-1) violated: sbx.terminate() bypassed batch Lease fencing after "
            "batch.detach() and called delete_sandbox_claim directly!",
        ):
            sbx.terminate()
        helper.delete_sandbox_claim.assert_not_called()

    def test_batch_reaper_restarts_on_410_pagination_expiry(self) -> None:
        """Reproduces F-18a: `batch_reaper.find_stale_leases` does not restart pagination on
        `410 Gone` when a `continue` token expires, aborting the entire namespace.
        """
        now = datetime(2026, 11, 7, 3, 0, 0, tzinfo=UTC)
        expired_time = now - timedelta(seconds=600)
        valid_stale = k8s_client.V1Lease(
            metadata=k8s_client.V1ObjectMeta(
                name="batch-goodstale",
                resource_version="44",
                labels={BATCH_ID_LABEL: "goodstale"},
            ),
            spec=k8s_client.V1LeaseSpec(
                holder_identity="dead-holder",
                renew_time=expired_time,
                lease_duration_seconds=60,
            ),
        )
        helper: Any = MagicMock()
        page_1 = MagicMock(items=[], metadata=MagicMock(_continue="tok-expired"))
        page_restart = MagicMock(items=[valid_stale], metadata=MagicMock(_continue=None))
        helper.coordination_v1_api.list_namespaced_lease.side_effect = [
            page_1,
            k8s_client.ApiException(status=410, reason="Gone"),
            page_restart,
        ]

        found = batch_reaper.find_stale_leases(helper, "default", now, skew_margin=30)
        self.assertEqual([s.batch_id for s in found], ["goodstale"])

    def test_batch_reaper_isolates_malformed_naive_renew_time(self) -> None:
        """Reproduces F-18b: A single Lease with a naive `spec.renew_time` raises `TypeError`
        inside `find_stale_leases`, aborting reaping of all other valid expired Leases in the
        same namespace.
        """
        now = datetime(2026, 11, 7, 3, 0, 0, tzinfo=UTC)
        expired_time = now - timedelta(seconds=600)
        naive_time = datetime(2026, 11, 7, 1, 0, 0)

        valid_stale = k8s_client.V1Lease(
            metadata=k8s_client.V1ObjectMeta(
                name="batch-goodstale",
                resource_version="44",
                labels={BATCH_ID_LABEL: "goodstale"},
            ),
            spec=k8s_client.V1LeaseSpec(
                holder_identity="dead-holder",
                renew_time=expired_time,
                lease_duration_seconds=60,
            ),
        )
        naive_lease = k8s_client.V1Lease(
            metadata=k8s_client.V1ObjectMeta(
                name="batch-naivetime",
                resource_version="45",
                labels={BATCH_ID_LABEL: "naivetime"},
            ),
            spec=k8s_client.V1LeaseSpec(
                holder_identity="other-holder",
                renew_time=naive_time,
                lease_duration_seconds=60,
            ),
        )
        helper: Any = MagicMock()
        helper.coordination_v1_api.list_namespaced_lease.return_value = MagicMock(
            items=[naive_lease, valid_stale],
            metadata=MagicMock(_continue=None),
        )

        found = batch_reaper.find_stale_leases(helper, "default", now, skew_margin=30)
        self.assertEqual([s.batch_id for s in found], ["goodstale"])

    def test_lease_replace_404_toctou_in_release_get_batch_and_detach(self) -> None:
        """Reproduces F-29 (INV-OWN-3 / INV-LEAK-2): When `batch_reaper` or a racing client
        deletes the Lease between `read_batch_lease` (GET) and `replace_batch_lease` (PUT),
        `replace_batch_lease` raises `ApiException(status=404)`. `_take_batch_lease` (`release`),
        `_attach` (`get_batch`), and `detach` only catch `409`, letting raw `ApiException(404)`
        abort `release()` before `deletecollection`, escape `except BatchError:` in `get_batch()`,
        and leave `_lease_released = False` in `detach()`.
        """
        # 1. release() -> _take_batch_lease must treat 404 as missing Lease and run deletecollection
        _client1, helper1, batch1 = _new_stopped_sync_batch()
        batch1._state.upsert_claim(_claim_obj("b1-0"))
        helper1.replace_batch_lease.side_effect = k8s_client.ApiException(
            status=404, reason="Not Found"
        )
        batch1.release()
        helper1.delete_sandbox_claims_by_label.assert_called_once()
        self.assertTrue(batch1._released)

        # 2. get_batch() -> _attach must map 404 to BatchNotFoundError / BatchLeaseExpiredError
        with (
            patch("k8s_agent_sandbox.sandbox_client.K8sHelper"),
            patch("k8s_agent_sandbox.sandbox_client.atexit.register"),
        ):
            attach_client: SandboxClient[Any] = SandboxClient(cleanup=False)
        attach_helper: Any = attach_client.k8s_helper
        attach_helper.api_pool_size.return_value = 25
        attach_helper.read_batch_lease.side_effect = lambda *_a, **_kw: _make_lease(
            holder_identity="", resource_version="10", batch_id="b1"
        )
        attach_helper.replace_batch_lease.side_effect = k8s_client.ApiException(
            status=404, reason="Not Found"
        )
        attach_helper.list_sandbox_claim_objects.return_value = ([], "5")
        with self.assertRaises(BatchNotFoundError):
            attach_client.get_batch("b1")

        attach_helper.list_sandbox_claim_objects.return_value = ([_claim_obj("b1-0")], "6")
        with self.assertRaises(BatchLeaseExpiredError):
            attach_client.get_batch("b1")

        # 3. detach() must treat external 404 on replace_batch_lease as already released
        _client3, helper3, batch3 = _new_stopped_sync_batch()
        helper3.replace_batch_lease.side_effect = k8s_client.ApiException(
            status=404, reason="Not Found"
        )
        batch3.detach(grace=30)
        self.assertTrue(batch3._lease_released)


class TestCell12AsyncClientTeardownAndCloseRace(unittest.IsolatedAsyncioTestCase):
    """Cell 12 (Async): F-08 AsyncSandboxClient.__aexit__ failed-batch retention,
    F-15 close() vs concurrent batch.release() serialization, and async twins for
    F-24, F-27a, F-27b, and F-29.
    """

    async def test_async_client_aexit_retains_failed_batch_for_cleanup_retry(self) -> None:
        """Reproduces F-08 (INV-LEAK-1): When `AsyncSandboxClient(cleanup=True)` exits an
        `async with` block and `batch.release()` fails (e.g. RBAC `403` before token refresh),
        `close()` calls `_stop_batches()` which unconditionally calls `_unregister_batch(batch)`.
        That drops the failed batch from `_active_batches` without moving it to `_closing_batches`,
        so a subsequent `await client.delete_all()` retry deletes 0 claims and 0 Leases!
        """
        client, helper, batch = await _new_stopped_async_batch(cleanup=True)
        try:
            batch._state.upsert_claim(_claim_obj("b1-0"))
            helper.count_live_sandbox_claims = AsyncMock(return_value=0)

            # First delete_sandbox_claims_by_label fails with 403 during __aexit__, then succeeds.
            helper.delete_sandbox_claims_by_label = AsyncMock(
                side_effect=[
                    async_k8s_client.ApiException(status=403, reason="Forbidden"),
                    None,
                ]
            )

            await client.__aexit__(None, None, None)

            # Re-attempt cleanup via delete_all(): must retry releasing the failed batch!
            await client.delete_all()
            self.assertEqual(
                helper.delete_sandbox_claims_by_label.await_count,
                2,
                "F-08 (INV-LEAK-1) violated: AsyncSandboxClient.__aexit__ -> close() -> "
                "_stop_batches() unconditionally unregistered the failed batch, so delete_all() "
                "never retried it!",
            )
            helper.delete_batch_lease.assert_awaited_once()
        finally:
            client._active_batches.clear()
            client._closing_batches.clear()

    async def test_async_client_close_serializes_with_in_flight_batch_release(self) -> None:
        """Reproduces F-15: If `await batch.release()` is mid-flight under `batch._lifecycle_lock`
        when `await client.close()` runs concurrently, `_stop_batches()` sets
        `batch._detached = True` without acquiring `batch._lifecycle_lock` and `close()`
        immediately closes `k8s_helper` while `batch.release()` is still issuing K8s Lease/claim
        delete calls!
        """
        client, helper, batch = await _new_stopped_async_batch(cleanup=False)
        batch._state.upsert_claim(_claim_obj("b1-0"))

        entered_delete = asyncio.Event()
        allow_delete_finish = asyncio.Event()
        helper_closed = False

        async def slow_delete_by_label(*_args: Any, **_kwargs: Any) -> None:
            entered_delete.set()
            await allow_delete_finish.wait()
            if helper_closed:
                raise RuntimeError("Session is closed while batch.release() is still running!")

        async def mark_helper_closed() -> None:
            nonlocal helper_closed
            helper_closed = True

        helper.delete_sandbox_claims_by_label = AsyncMock(side_effect=slow_delete_by_label)
        helper.delete_batch_lease = AsyncMock(
            side_effect=lambda *_a, **_kw: (
                (_ for _ in ()).throw(RuntimeError("Session is closed!")) if helper_closed else None
            )
        )
        helper.close = AsyncMock(side_effect=mark_helper_closed)

        release_task = asyncio.create_task(batch.release())
        await asyncio.wait_for(entered_delete.wait(), timeout=1.0)

        close_task = asyncio.create_task(client.close())
        await asyncio.sleep(0.03)
        allow_delete_finish.set()

        await asyncio.gather(release_task, close_task)
        helper.delete_batch_lease.assert_awaited_once()

    async def test_async_release_drains_in_flight_acquire_create_without_leaking_claim(
        self,
    ) -> None:
        """Reproduces F-24 (INV-LEAK-1 / INV-OWN-3) on AsyncSandboxBatch: `acquire()` sends
        `create_sandbox_claim` outside `self._creators` and `_active_deletes`, so a concurrent
        `await batch.release()` finishes `deletecollection` and sets `self._detached = True`
        while `create_sandbox_claim` is mid-POST, stranding the newly created SandboxClaim!
        """
        _client, helper, batch = await _new_stopped_async_batch([
            BatchGroup(warmpool="pool-a", size=0)
        ])
        entered_post = asyncio.Event()
        allow_post_return = asyncio.Event()
        live_claims: set[str] = set()

        async def slow_create(
            claim_name: str, warmpool: str, *_args: Any, **_kwargs: Any
        ) -> dict[str, Any]:
            entered_post.set()
            await allow_post_return.wait()
            live_claims.add(claim_name)
            return _claim_obj(
                claim_name, warmpool=warmpool, ready="False", reason="Starting", sandbox_name=None
            )

        async def delete_single(claim_name: str, *_args: Any, **_kwargs: Any) -> bool:
            live_claims.discard(claim_name)
            return True

        async def delete_by_label(*_args: Any, **_kwargs: Any) -> None:
            live_claims.clear()

        helper.create_sandbox_claim = AsyncMock(side_effect=slow_create)
        helper.delete_sandbox_claim = AsyncMock(side_effect=delete_single)
        helper.delete_sandbox_claims_by_label = AsyncMock(side_effect=delete_by_label)

        acq_task = asyncio.create_task(batch.acquire("pool-a", timeout=5.0))
        await asyncio.wait_for(entered_post.wait(), timeout=1.0)
        rel_task = asyncio.create_task(batch.release())
        await asyncio.sleep(0.05)
        allow_post_return.set()

        acq_res, _ = await asyncio.gather(acq_task, rel_task, return_exceptions=True)
        await batch._wait_active_deletes()
        self.assertIsInstance(acq_res, BaseException)
        self.assertEqual(
            live_claims,
            set(),
            f"F-24 (INV-LEAK-1) violated: AsyncSandboxBatch.acquire() POST escaped release() "
            f"barrier and leaked live SandboxClaim(s) {live_claims}!",
        )

    async def test_async_get_batch_fences_slow_attach_lease_write(self) -> None:
        """Reproduces F-27a (INV-AUTH-3) on AsyncSandboxClient: `await client.get_batch()`
        returns an already-expired handle when `replace_batch_lease` exceeds `lease_duration`.
        """
        with (
            patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper"),
            patch("k8s_agent_sandbox.async_sandbox_client.atexit.register"),
        ):
            norm_client: AsyncSandboxClient[Any] = AsyncSandboxClient(
                connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
                cleanup=False,
            )
        norm_helper: Any = norm_client.k8s_helper
        norm_helper.api_pool_size = AsyncMock(return_value=25)
        slow_lease = _make_async_lease(
            holder_identity="",
            resource_version="10",
            lease_duration_seconds=6,
            batch_id="b1",
        )
        assert slow_lease.metadata is not None
        slow_lease.metadata.annotations = {BATCH_LEASE_DURATION_ANNOTATION: "6"}
        norm_helper.read_batch_lease = AsyncMock(return_value=slow_lease)
        norm_helper.list_sandbox_claim_objects = AsyncMock(return_value=([], "5"))
        fake_clock = [1000.0]

        async def slow_replace(
            _n: str, _ns: str, body: Any, **_kw: Any
        ) -> async_k8s_client.V1Lease:
            fake_clock[0] += 7.0
            return _make_async_lease(
                holder_identity=body.spec.holder_identity,
                resource_version="11",
                lease_duration_seconds=6,
            )

        norm_helper.replace_batch_lease = AsyncMock(side_effect=slow_replace)
        with (
            patch(
                "k8s_agent_sandbox.async_sandbox_batch.time.monotonic",
                side_effect=lambda: fake_clock[0],
            ),
            patch.object(AsyncSandboxBatch, "_start", lambda _s: None),
            self.assertRaises(
                BatchLeaseExpiredError,
                msg="F-27a (INV-AUTH-3) violated: AsyncSandboxClient.get_batch() returned an "
                "already-expired handle after replace_batch_lease took 7s on a 6s Lease!",
            ),
        ):
            await norm_client.get_batch("b1")

    async def test_async_get_batch_validates_and_clamps_pool_size(self) -> None:
        """Reproduces F-27b on AsyncSandboxClient: `await client.get_batch()` skips
        `batch_utils.check_pool_size`, allowing `pool_size=2` and starving Lease renewal.
        """
        with (
            patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper"),
            patch("k8s_agent_sandbox.async_sandbox_client.atexit.register"),
        ):
            small_client: AsyncSandboxClient[Any] = AsyncSandboxClient(
                connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
                cleanup=False,
            )
        small_helper: Any = small_client.k8s_helper
        small_helper.api_pool_size = AsyncMock(return_value=2)
        detached_lease = _make_async_lease(
            holder_identity="",
            resource_version="10",
            lease_duration_seconds=6,
            batch_id="b1",
        )
        assert detached_lease.metadata is not None
        detached_lease.metadata.annotations = {BATCH_LEASE_DURATION_ANNOTATION: "6"}
        small_helper.read_batch_lease = AsyncMock(return_value=detached_lease)
        small_helper.list_sandbox_claim_objects = AsyncMock(return_value=([], "5"))
        small_helper.replace_batch_lease = AsyncMock(
            return_value=_make_async_lease(
                holder_identity="new-h", resource_version="11", lease_duration_seconds=6
            )
        )
        with (
            patch.object(AsyncSandboxBatch, "_start", lambda _s: None),
            self.assertRaises(
                ValueError,
                msg="F-27b violated: AsyncSandboxClient.get_batch() skipped check_pool_size() "
                "and allowed pool_size=2!",
            ),
        ):
            await small_client.get_batch("b1")

    async def test_async_lease_replace_404_toctou_in_release_get_batch_and_detach(self) -> None:
        """Reproduces F-29 (INV-OWN-3 / INV-LEAK-2) on AsyncSandboxBatch / AsyncSandboxClient:
        `replace_batch_lease` returning `ApiException(status=404)` in the TOCTOU window after
        `read_batch_lease` aborts `release()`, escapes `get_batch()`, and fails `detach()`.
        """
        # 1. release() -> _take_batch_lease must treat 404 as missing Lease and run deletecollection
        _client1, helper1, batch1 = await _new_stopped_async_batch()
        batch1._state.upsert_claim(_claim_obj("b1-0"))
        helper1.replace_batch_lease = AsyncMock(
            side_effect=async_k8s_client.ApiException(status=404, reason="Not Found")
        )
        await batch1.release()
        helper1.delete_sandbox_claims_by_label.assert_awaited_once()
        self.assertTrue(batch1._released)

        # 2. get_batch() -> _attach must map 404 to BatchNotFoundError / BatchLeaseExpiredError
        with (
            patch("k8s_agent_sandbox.async_sandbox_client.AsyncK8sHelper"),
            patch("k8s_agent_sandbox.async_sandbox_client.atexit.register"),
        ):
            attach_client: AsyncSandboxClient[Any] = AsyncSandboxClient(
                connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:8080"),
                cleanup=False,
            )
        attach_helper: Any = attach_client.k8s_helper
        attach_helper.api_pool_size = AsyncMock(return_value=25)
        attach_helper.read_batch_lease = AsyncMock(
            side_effect=lambda *_a, **_kw: _make_async_lease(
                holder_identity="", resource_version="10", batch_id="b1"
            )
        )
        attach_helper.replace_batch_lease = AsyncMock(
            side_effect=async_k8s_client.ApiException(status=404, reason="Not Found")
        )
        attach_helper.list_sandbox_claim_objects = AsyncMock(return_value=([], "5"))
        with self.assertRaises(BatchNotFoundError):
            await attach_client.get_batch("b1")

        attach_helper.list_sandbox_claim_objects = AsyncMock(
            return_value=([_claim_obj("b1-0")], "6")
        )
        with self.assertRaises(BatchLeaseExpiredError):
            await attach_client.get_batch("b1")

        # 3. detach() must treat external 404 on replace_batch_lease as already released
        _client3, helper3, batch3 = await _new_stopped_async_batch()
        helper3.replace_batch_lease = AsyncMock(
            side_effect=async_k8s_client.ApiException(status=404, reason="Not Found")
        )
        await batch3.detach(grace=30)
        self.assertTrue(batch3._lease_released)


if __name__ == "__main__":
    unittest.main()
