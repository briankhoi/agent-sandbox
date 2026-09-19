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
"""AsyncSandboxBatch: the async handle for a claimed or re-attached batch.

Requires the ``async`` optional dependencies::

    pip install k8s-agent-sandbox[async]
"""

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp
from kubernetes_asyncio import client

from . import batch_state
from .constants import BATCH_ID_LABEL, BATCH_LEASE_DURATION_ANNOTATION, BATCH_LEASE_NAME_PREFIX
from .exceptions import (
    BatchError,
    BatchInUseError,
    BatchLeaseExpiredError,
    BatchNotFoundError,
    SandboxNotReadyError,
)
from .models import BatchGroup, Member

if TYPE_CHECKING:
    from .async_sandbox import AsyncSandbox
    from .async_sandbox_client import AsyncSandboxClient

logger = logging.getLogger(__name__)

# Each watch attempt is bounded so the watcher task periodically has a
# chance to observe cancellation instead of blocking indefinitely.
_WATCH_SEGMENT_SECONDS = 30


class AsyncSandboxBatch:
    """Async handle to an existing batch's claims, obtained via
    ``AsyncSandboxClient.get_batch``.

    Keeps a live cache of the batch's claims through one label-scoped watch
    (a background asyncio task), renews the batch Lease on another task, and
    exposes ``members()``, ``connect()``, ``err()``, and ``detach()``.
    """

    def __init__(
        self,
        client: "AsyncSandboxClient",
        batch_id: str,
        namespace: str,
        state: batch_state.BatchState,
        lease_name: str,
        holder_identity: str,
        lease_duration: int,
        list_resource_version: str,
    ) -> None:
        self._client = client
        self.batch_id = batch_id
        self.namespace = namespace
        self._state = state
        self._lease_name = lease_name
        self._holder_identity = holder_identity
        self._lease_duration = lease_duration
        self._list_resource_version = list_resource_version

        self._lock = asyncio.Lock()
        self._connected: dict[str, "AsyncSandbox"] = {}
        self._detached = False
        self._renewal_degraded = False
        self._last_renew_success = time.monotonic()
        self._watch_task: asyncio.Task | None = None
        self._renew_task: asyncio.Task | None = None

    @property
    def groups(self) -> list[BatchGroup]:
        return list(self._state.groups)

    @property
    def size(self) -> int:
        return self._state.size

    @classmethod
    async def _attach(
        cls, client: "AsyncSandboxClient", batch_id: str, namespace: str
    ) -> "AsyncSandboxBatch":
        """Implements ``get_batch``: attach to an existing batch and take
        over its Lease, per the plan's 10-step sequence."""
        batch_state.validate_batch_id(batch_id)

        lease_name = f"{BATCH_LEASE_NAME_PREFIX}{batch_id}"
        label_selector = f"{BATCH_ID_LABEL}={batch_id}"

        lease = await client.k8s_helper.read_batch_lease(lease_name, namespace)
        claim_items, list_rv = await client.k8s_helper.list_sandbox_claim_objects(
            namespace, label_selector
        )

        annotation = None
        if lease is not None:
            annotation = (lease.metadata.annotations or {}).get(BATCH_LEASE_DURATION_ANNOTATION)
        duration = batch_state.parse_lease_duration_annotation(annotation)

        if lease is None and not claim_items:
            raise BatchNotFoundError(f"batch '{batch_id}' not found in namespace '{namespace}'")

        now = datetime.now(UTC)
        if lease is None or batch_state.is_lease_stale(lease.spec.renew_time, duration, now):
            raise BatchLeaseExpiredError(
                f"batch '{batch_id}' Lease is missing or stale in namespace '{namespace}'"
            )

        if lease.spec.holder_identity is not None:
            raise BatchInUseError(
                f"batch '{batch_id}' is held by '{lease.spec.holder_identity}'"
            )

        holder_identity = batch_state.generate_holder_identity()
        lease.spec.holder_identity = holder_identity
        lease.spec.renew_time = now
        lease.spec.lease_duration_seconds = duration
        await client.k8s_helper.replace_batch_lease(lease_name, namespace, lease)

        groups = batch_state.reconstruct_groups(claim_items)
        state = batch_state.BatchState(batch_id, groups)
        state.seed_from_claims(claim_items)

        handle = cls(
            client=client,
            batch_id=batch_id,
            namespace=namespace,
            state=state,
            lease_name=lease_name,
            holder_identity=holder_identity,
            lease_duration=duration,
            list_resource_version=list_rv,
        )
        handle._start()
        return handle

    def _start(self) -> None:
        self._watch_task = asyncio.ensure_future(self._watch_loop())
        self._renew_task = asyncio.ensure_future(self._renew_loop())

    async def _stop_background_tasks(self) -> None:
        """Cancels the watcher and renewal tasks without touching the Lease.

        Used by ``AsyncSandboxClient.close()``, distinct from ``detach()``,
        which also performs the final Lease update and unregisters.
        """
        for task in (self._watch_task, self._renew_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._watch_task = None
        self._renew_task = None

    def _check_active(self) -> None:
        if self._detached:
            raise BatchError(f"batch '{self.batch_id}' has been detached")

    async def members(self, warmpool: str | None = None) -> list[Member]:
        async with self._lock:
            return self._state.members(warmpool)

    async def connect(self, member: Member) -> "AsyncSandbox":
        async with self._lock:
            self._check_active()
            cached = self._connected.get(member.claim_name)
            if cached is not None:
                return cached
            current = self._state.get(member.claim_name)
            if current is None or not current.ready:
                raise SandboxNotReadyError(
                    f"batch member '{member.claim_name}' is not ready"
                )
            sandbox = self._client.sandbox_class(
                claim_name=member.claim_name,
                sandbox_id=current.sandbox_name,
                namespace=self.namespace,
                connection_config=self._client.connection_config,
                tracer_config=self._client.tracer_config,
                k8s_helper=self._client.k8s_helper,
            )
            self._connected[member.claim_name] = sandbox
            return sandbox

    async def err(self) -> Exception | None:
        async with self._lock:
            return self._state.error()

    async def detach(self, grace: int | None = None) -> None:
        async with self._lock:
            if self._detached:
                raise BatchError(f"batch '{self.batch_id}' has already been detached")
        if grace is not None:
            batch_state.validate_lease_duration_value(grace)

        async with self._lock:
            self._detached = True

        await self._stop_background_tasks()

        async with self._lock:
            connected = list(self._connected.values())
            self._connected.clear()
        for sandbox in connected:
            await sandbox.close_connection()

        lease = await self._client.k8s_helper.read_batch_lease(self._lease_name, self.namespace)
        if lease is not None:
            lease.spec.holder_identity = None
            lease.spec.renew_time = datetime.now(UTC)
            lease.spec.lease_duration_seconds = (
                grace if grace is not None else self._lease_duration
            )
            await self._client.k8s_helper.replace_batch_lease(
                self._lease_name, self.namespace, lease
            )

        self._client._unregister_batch(self.namespace, self.batch_id)

    async def _watch_loop(self) -> None:
        rv = self._list_resource_version
        label_selector = f"{BATCH_ID_LABEL}={self.batch_id}"
        while True:
            try:
                async for event in self._client.k8s_helper.watch_sandbox_claims(
                    self.namespace, label_selector, rv, _WATCH_SEGMENT_SECONDS
                ):
                    event_type = event.get("type")
                    obj = event.get("object") or {}
                    seen_rv = (obj.get("metadata") or {}).get("resourceVersion")
                    if seen_rv:
                        rv = seen_rv
                    if event_type == "BOOKMARK":
                        continue
                    async with self._lock:
                        if event_type == "DELETED":
                            self._state.mark_deleted((obj.get("metadata") or {}).get("name", ""))
                        elif event_type in ("ADDED", "MODIFIED"):
                            self._state.apply_claim(obj)
            except asyncio.CancelledError:
                raise
            except client.ApiException as e:
                if e.status == 410:
                    try:
                        items, rv = await self._client.k8s_helper.list_sandbox_claim_objects(
                            self.namespace, label_selector
                        )
                    except client.ApiException as list_exc:
                        async with self._lock:
                            self._state.note_error(list_exc)
                        return
                    async with self._lock:
                        self._state.reconcile_list(items)
                    continue
                async with self._lock:
                    self._state.note_error(e)
                return
            except aiohttp.ClientSSLError:
                raise
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                ConnectionError,
            ):
                await asyncio.sleep(0.5)
                continue

    async def _renew_loop(self) -> None:
        interval = max(1, self._lease_duration // 3)
        while True:
            await asyncio.sleep(interval)
            await self._renew_once()

    async def _renew_once(self) -> None:
        try:
            lease = await self._client.k8s_helper.read_batch_lease(self._lease_name, self.namespace)
            if lease is None:
                raise BatchLeaseExpiredError(
                    f"batch '{self.batch_id}' Lease '{self._lease_name}' no longer exists"
                )
            lease.spec.holder_identity = self._holder_identity
            lease.spec.renew_time = datetime.now(UTC)
            lease.spec.lease_duration_seconds = self._lease_duration
            await self._client.k8s_helper.replace_batch_lease(
                self._lease_name, self.namespace, lease
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            async with self._lock:
                if not self._renewal_degraded:
                    logger.info(f"Batch '{self.batch_id}' lease renewal degraded: {e}")
                    self._renewal_degraded = True
                if time.monotonic() - self._last_renew_success >= self._lease_duration:
                    self._state.note_error(
                        BatchLeaseExpiredError(
                            f"batch '{self.batch_id}' lease has not renewed successfully "
                            f"for {self._lease_duration}s"
                        )
                    )
            return
        async with self._lock:
            self._last_renew_success = time.monotonic()
            self._renewal_degraded = False
