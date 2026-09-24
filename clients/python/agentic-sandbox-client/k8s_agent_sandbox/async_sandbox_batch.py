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

"""Async handle for using a claimed or re-attached sandbox batch.

Requires the ``async`` optional dependencies::

    pip install k8s-agent-sandbox[async]
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp
from kubernetes_asyncio import client
from kubernetes_asyncio.client import V1Lease, V1LeaseSpec, V1ObjectMeta
from kubernetes_asyncio.client.exceptions import ApiException

from . import batch_state, batch_utils
from .batch_state import (
    BATCH_DEFAULT_CREATE_RPS,
    BATCH_DEFAULT_MAX_IN_FLIGHT,
    BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS,
    BATCH_DEFAULT_SHUTDOWN_MARGIN_SECONDS,
    BATCH_DEFAULT_WORK_BUDGET_SECONDS,
    BATCH_RELEASE_MAX_DELETE_ROUNDS,
    BATCH_RELEASE_RELIST_INTERVAL_SECONDS,
)
from .constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    BATCH_ID_LABEL,
    BATCH_LEASE_DURATION_ANNOTATION,
    BATCH_LEASE_NAME_PREFIX,
    BATCH_QUORUM_TIMEOUT_ANNOTATION,
)
from .exceptions import (
    BatchError,
    BatchExistsError,
    BatchInUseError,
    BatchLeaseExpiredError,
    BatchNotFoundError,
    SandboxNotReadyError,
    SandboxTemplateNotFoundError,
)
from .models import BatchEvent, BatchGroup, GroupReady, Member
from .utils import construct_sandbox_claim_lifecycle_spec

if TYPE_CHECKING:
    from .async_sandbox import AsyncSandbox
    from .async_sandbox_client import AsyncSandboxClient

logger = logging.getLogger(__name__)

# Each watch attempt has this bounded timeout so the watcher task periodically has a
# chance to observe cancellation instead of blocking indefinitely.
_WATCH_TIMEOUT_SECONDS = 30

# Transport-level failures on a claim create. The request may or may not have landed, so they
# are retried like a 5xx; a retry that then gets a 409 counts as success.
_CREATE_TRANSPORT_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
    ConnectionError,
)


async def _get_for_precheck(
    get: Callable[[str, str], Awaitable[dict]], kind: str, name: str, namespace: str
) -> dict | None:
    """Runs one dependency-precheck GET for ``claim_batch``, or returns ``None`` when the driver
    Role lacks ``get`` on it (403): the precheck is then skipped rather than failing a batch that
    would otherwise work.
    """
    try:
        return await get(name, namespace)
    except ApiException as e:
        if e.status != 403:
            raise
        logger.warning(
            f"Cannot precheck {kind} '{name}' in namespace '{namespace}' "
            f"(403 Forbidden); skipping its dependency check"
        )
        return None


class AsyncSandboxBatch:
    """An async handle to a batch's claims, obtained via ``AsyncSandboxClient.claim_batch`` or
    ``AsyncSandboxClient.get_batch``.

    Keeps a live cache of the batch's claims through one label-scoped watch
    (a background asyncio task), renews the batch Lease on another task, and
    exposes methods for consuming, connecting to, and releasing the batch's sandboxes.
    A handle from ``claim_batch`` also creates the batch's claims on a third task.
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
        *,
        work_budget: int = BATCH_DEFAULT_WORK_BUDGET_SECONDS,
        quorum_timeout: int = BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS,
        create_rps: float = BATCH_DEFAULT_CREATE_RPS,
        max_in_flight: int = BATCH_DEFAULT_MAX_IN_FLIGHT,
        claim_labels: Mapping[str, str] | None = None,
        claim_annotations: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.batch_id = batch_id
        self.namespace = namespace
        self._state = state
        self._lease_name = lease_name
        self._holder_identity = holder_identity
        self._lease_duration = lease_duration
        self._list_resource_version = list_resource_version
        # On attach, quorum_timeout is parsed from the Lease annotations so a re-attached handle
        # keeps the batch's fill deadline.
        self._work_budget = work_budget
        self._quorum_timeout = quorum_timeout
        self._claim_labels = {**(claim_labels or {}), BATCH_ID_LABEL: batch_id}
        self._claim_annotations = dict(claim_annotations or {})
        self._clock = clock

        self._lock = asyncio.Lock()
        # Shares _lock, so every state change can wake the events() and iter_ready_groups() consumers.
        self._cond = asyncio.Condition(self._lock)
        self._connected: dict[str, "AsyncSandbox"] = {}
        self._detached = False
        self._lease_released = False
        self._renewal_degraded = False
        self._last_renew_success = time.monotonic()
        self._watch_task: asyncio.Task | None = None
        self._renew_task: asyncio.Task | None = None

        self._create_task: asyncio.Task | None = None
        self._pacer = batch_state.CreatePacer(create_rps)
        self._in_flight_slots = asyncio.Semaphore(max_in_flight)
        self._cancelled_pools: set[str] = set()
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

        self._release_lock = asyncio.Lock()
        self._releasing = False
        self._release_done = False

        self._state.set_fill_deadline(self._clock() + quorum_timeout)

    @property
    def groups(self) -> list[BatchGroup]:
        """The batch's ``BatchGroup``\\ s; one per warm pool."""
        return list(self._state.groups)

    @property
    def size(self) -> int:
        """The total number of claims across all groups."""
        return self._state.size

    @classmethod
    async def _attach(cls, client: "AsyncSandboxClient", batch_id: str, namespace: str) -> "AsyncSandboxBatch":
        """Attach to an existing batch and take over its Lease."""
        batch_utils.validate_batch_id(batch_id)

        lease_name = f"{BATCH_LEASE_NAME_PREFIX}{batch_id}"
        label_selector = f"{BATCH_ID_LABEL}={batch_id}"

        lease = await client.k8s_helper.read_batch_lease(lease_name, namespace)
        claim_items, list_rv = await client.k8s_helper.list_sandbox_claim_objects(namespace, label_selector)

        if lease is None and not claim_items:
            raise BatchNotFoundError(f"batch '{batch_id}' not found in namespace '{namespace}'")

        lease_annotations: dict[str, str] = {}
        if lease is not None:
            lease_annotations = lease.metadata.annotations or {}
        duration = batch_utils.parse_lease_duration_annotation(
            lease_annotations.get(BATCH_LEASE_DURATION_ANNOTATION)
        )
        quorum_timeout = batch_utils.parse_positive_int_annotation(
            lease_annotations.get(BATCH_QUORUM_TIMEOUT_ANNOTATION),
            BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS,
            "quorum timeout",
        )

        now = datetime.now(UTC)
        spec_duration = lease.spec.lease_duration_seconds if lease is not None else None
        renew_time = lease.spec.renew_time if lease is not None else None
        if (
            lease is None
            or renew_time is None
            or spec_duration is None
            or batch_state.is_lease_stale(renew_time, spec_duration, now)
        ):
            raise BatchLeaseExpiredError(
                f"batch '{batch_id}' Lease is missing or stale in namespace '{namespace}'"
            )

        if lease.spec.holder_identity is not None:
            raise BatchInUseError(
                f"batch '{batch_id}' is held by '{lease.spec.holder_identity}'"
            )

        groups = batch_state.reconstruct_groups(claim_items, batch_id)
        state = batch_state.BatchState(batch_id, groups)
        state.seed_from_claims(claim_items)

        holder_identity = batch_utils.generate_holder_identity()
        lease.spec.holder_identity = holder_identity
        lease.spec.renew_time = now
        lease.spec.lease_duration_seconds = duration
        try:
            await client.k8s_helper.replace_batch_lease(lease_name, namespace, lease)
        except ApiException as e:
            if e.status == 409:
                raise BatchInUseError(
                    f"batch '{batch_id}' Lease was taken over by another client while attaching"
                ) from e
            raise

        handle = cls(
            client=client,
            batch_id=batch_id,
            namespace=namespace,
            state=state,
            lease_name=lease_name,
            holder_identity=holder_identity,
            lease_duration=duration,
            list_resource_version=list_rv,
            quorum_timeout=quorum_timeout,
        )
        handle._start()
        return handle

    @classmethod
    async def _claim(
        cls,
        client: "AsyncSandboxClient",
        groups: Sequence[BatchGroup],
        *,
        namespace: str,
        labels: Mapping[str, str] | None,
        batch_id: str | None,
        create_rps: float | None,
        max_in_flight: int | None,
        work_budget: int | None,
        quorum_timeout: int | None,
        lease_duration: int | None,
        claim_annotations: Mapping[str, str] | None = None,
    ) -> "AsyncSandboxBatch":
        """Creates a batch: its Lease, a watch, Lease renewal, and background claim creation."""
        args = batch_utils.validate_claim_batch_args(
            groups, labels, batch_id, create_rps, max_in_flight, work_budget, quorum_timeout, lease_duration
        )

        # Check each group's dependencies the same way the controller resolves them, so a missing
        # or misspelled pool or template fails here, before anything exists. At runtime the
        # controller retries WarmPoolNotFound/TemplateNotFound, so batch members don't treat them
        # as terminal (OPEN-W).
        for group in args.groups:
            warmpool = await _get_for_precheck(
                client.k8s_helper.get_sandbox_warmpool, "SandboxWarmPool", group.warmpool, namespace
            )
            if warmpool is None:
                continue
            template_name = batch_utils.warmpool_template_name(warmpool)
            if not template_name:
                raise SandboxTemplateNotFoundError(
                    f"SandboxWarmPool '{group.warmpool}' in namespace '{namespace}' names no SandboxTemplate"
                )
            await _get_for_precheck(
                client.k8s_helper.get_sandbox_template, "SandboxTemplate", template_name, namespace
            )

        lease_name = f"{BATCH_LEASE_NAME_PREFIX}{args.batch_id}"
        holder_identity = batch_utils.generate_holder_identity()
        now = datetime.now(UTC)
        lease_labels, lease_annotations = batch_utils.batch_lease_metadata(args)
        lease = V1Lease(
            metadata=V1ObjectMeta(
                name=lease_name, namespace=namespace, labels=lease_labels, annotations=lease_annotations
            ),
            spec=V1LeaseSpec(
                holder_identity=holder_identity,
                lease_duration_seconds=args.lease_duration,
                acquire_time=now,
                renew_time=now,
            ),
        )
        try:
            await client.k8s_helper.create_batch_lease(namespace, lease)
        except ApiException as e:
            if e.status == 409:
                raise BatchExistsError(
                    f"batch '{args.batch_id}' already exists in namespace '{namespace}'"
                ) from e
            raise

        state = batch_state.BatchState(args.batch_id, args.groups)
        plan = state.plan_initial_fill()
        handle: "AsyncSandboxBatch | None" = None
        try:
            label_selector = f"{BATCH_ID_LABEL}={args.batch_id}"
            claim_items, list_rv = await client.k8s_helper.list_sandbox_claim_objects(
                namespace, label_selector
            )
            state.seed_from_claims(claim_items)
            handle = cls(
                client=client,
                batch_id=args.batch_id,
                namespace=namespace,
                state=state,
                lease_name=lease_name,
                holder_identity=holder_identity,
                lease_duration=args.lease_duration,
                list_resource_version=list_rv,
                work_budget=args.work_budget,
                quorum_timeout=args.quorum_timeout,
                create_rps=args.create_rps,
                max_in_flight=args.max_in_flight,
                claim_labels=args.labels,
                claim_annotations=claim_annotations,
            )
            handle._start()
        except BaseException:
            if handle is not None:
                await handle._stop_background_tasks()
            try:
                await client.k8s_helper.delete_batch_lease(lease_name, namespace)
            except Exception as delete_exc:
                logger.warning(
                    f"Batch '{args.batch_id}' failed to delete its Lease after a failed claim_batch: "
                    f"{delete_exc}"
                )
            raise

        handle._start_creation(plan)
        return handle

    def _start(self) -> None:
        self._watch_task = asyncio.ensure_future(self._watch_loop())
        self._renew_task = asyncio.ensure_future(self._renew_loop())

    async def _stop_background_tasks(self) -> None:
        """Cancels the watcher and lease renewal loop."""
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
        if self._releasing:
            raise BatchError(f"batch '{self.batch_id}' has been released")

    def members(self, warmpool: str | None = None) -> list[Member]:
        """Returns a snapshot of the batch's members, optionally filtered to one warm pool."""
        return self._state.members(warmpool)

    async def connect(self, member: Member) -> "AsyncSandbox":
        """Returns a connected ``AsyncSandbox`` for a ready member. If this handle is already connected
        to the Sandbox, it reuses the existing connection, otherwise it creates a new one and caches it for future calls.
        
        Raises ``SandboxNotReadyError`` if the member isn't ready.
        """
        async with self._lock:
            self._check_active()
            cached = self._connected.get(member.claim_name)
            if cached is not None:
                return cached
            # No name resolution or existence check as the batch's watch cache already knows the member's sandbox_name
            current = self._state.get_member(member.claim_name)
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

    def err(self) -> Exception | None:
        """Returns the error that stopped the batch's watch or background
        Lease renewal (e.g., ``BatchLeaseExpiredError``), or ``None`` while healthy.
        """
        return self._state.error()

    async def detach(self, grace: int | None = None) -> None:
        """Stops the background watch and Lease renewal, closes cached sandbox connections, and
        releases this handle's hold on the Lease. ``grace`` is the number of seconds to keep the
        Lease valid after detaching; if ``None``, we use the batch's original Lease duration.

        If the Lease release fails, the error propagates and this handle stays in a detached state with
        the Lease still held. Call ``detach`` again to retry; steps that already completed are skipped.
        """
        if grace is not None:
            batch_utils.validate_lease_duration_value(grace)

        # _detached prevents the caller from connecting to Sandboxes, while _lease_released signals that
        # detach fully completed (i.e. the previous steps and the Lease release write succeeded), so callers
        # can retry detach() if the release fails.
        async with self._lock:
            if self._lease_released:
                return
            if self._releasing:
                raise BatchError(f"batch '{self.batch_id}' has been released")
            self._detached = True

        # Claims already created stay for a later get_batch; the rest are not created.
        await self._stop_creation()
        await self._stop_background_tasks()
        async with self._cond:
            self._state.close()
            self._cond.notify_all()

        await self._close_cached_connections("detach")

        lease = await self._client.k8s_helper.read_batch_lease(self._lease_name, self.namespace)
        if lease is not None:
            if lease.spec.holder_identity != self._holder_identity:
                logger.info(
                    f"Batch '{self.batch_id}' Lease is held by "
                    f"'{lease.spec.holder_identity}', not this handle; treating as released"
                )
            else:
                lease.spec.holder_identity = None
                lease.spec.renew_time = datetime.now(UTC)
                # Refresh the Lease so it doesn't expire immediately after we release it,
                # giving other clients a chance to acquire it.
                lease.spec.lease_duration_seconds = (
                    grace if grace is not None else self._lease_duration
                )
                await self._client.k8s_helper.replace_batch_lease(
                    self._lease_name, self.namespace, lease
                )

        async with self._lock:
            self._lease_released = True

        self._client._unregister_batch(self.namespace, self.batch_id)

    def events(self) -> AsyncIterator[BatchEvent]:
        """Streams the initial fill's member transitions: ``MEMBER_READY``, ``MEMBER_FAILED`` (a
        terminal claim or a failed create), and ``MEMBER_LOST``, plus ``LEASE_DEGRADED`` once per
        episode of failing Lease renewals.

        Each Ready member is handed out at most once by this handle. If ``iter_ready_groups()``
        was called first, each group's first ``min_ready`` Ready members are held back for it, and
        only the rest stream here; if ``events()`` is called first, ``iter_ready_groups()`` then
        raises ``BatchError``.

        The stream ends once the initial fill settles (no member can still arrive, or
        ``quorum_timeout`` passed), or on ``release()``/``detach()``. Call it once and keep the
        iterator (``stream = batch.events()``) to stop and resume reading; a second ``events()``
        call raises ``BatchError``.
        """
        # No await between the check and the claim, so no other task can interleave.
        self._check_active()
        self._state.claim_events_consumer()
        return self._event_stream()

    async def _event_stream(self) -> AsyncIterator[BatchEvent]:
        while True:
            async with self._cond:
                while True:
                    if self._state.check_deadline(self._clock()):
                        self._cond.notify_all()
                    batch_events, done = self._state.collect_events()
                    if batch_events or done:
                        break
                    await self._wait_for_change(self._seconds_until_deadline())
            for event in batch_events:
                yield event
            if done:
                return

    def iter_ready_groups(self) -> AsyncIterator[GroupReady]:
        """Yields one ``GroupReady`` per group with ``size > 0``, as soon as that group's own
        quorum resolves, independently of the other groups:

        - ``min_ready`` Ready members (lowest ordinals first), handed out atomically;
        - or ``error=QuorumUnreachableError`` once too many members failed, were lost, or were
          released for the group to reach ``min_ready``;
        - or ``error=TimeoutError`` if it resolved neither way within ``quorum_timeout``.

        Ready members beyond ``min_ready`` are streamed by ``events()``, as are a group's
        held-back members when it yields an error. Call this before calling ``events()``.
        Raises ``BatchError`` if ``events()`` was called first, on a second call, and from the
        iterator if the batch is released or detached while it waits.
        """
        self._check_active()
        self._cancel_remaining_creates(self._state.claim_groups_consumer())
        return self._group_stream()

    async def _group_stream(self) -> AsyncIterator[GroupReady]:
        # Claiming the mode may have given verdicts that release held-back members to events().
        async with self._cond:
            self._cond.notify_all()
        while True:
            async with self._cond:
                while True:
                    if self._state.check_deadline(self._clock()):
                        self._cond.notify_all()
                    verdicts, done = self._state.pop_group_verdicts()
                    if verdicts or done:
                        break
                    await self._wait_for_change(self._seconds_until_deadline())
            for verdict in verdicts:
                yield verdict
            if done:
                return

    async def _wait_for_change(self, timeout: float | None) -> None:
        """Waits on the condition (whose lock the caller holds) for a notify or ``timeout``."""
        if timeout is None:
            await self._cond.wait()
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._cond.wait(), timeout)

    def _seconds_until_deadline(self) -> float | None:
        """How long a consumer may wait before the fill deadline needs checking; ``None`` (wait
        for a notify) once it has been applied.
        """
        deadline = self._state.pending_deadline()
        if deadline is None:
            return None
        return max(0.0, deadline - self._clock())

    async def release(self) -> None:
        """Deletes the whole batch: stops creation, the watch and Lease renewal, closes cached
        sandbox connections, deletes every claim with the batch label, then deletes the Lease.

        Idempotent. If a deletion fails (e.g. a 403 because the driver Role lacks
        ``deletecollection``), the error propagates and the Lease is left in place so a reaper
        can still clean up; ``events()`` and ``iter_ready_groups()`` end regardless, and calling
        ``release()`` again retries the deletion.
        """
        async with self._release_lock:
            async with self._lock:
                if self._release_done:
                    return
                if self._detached:
                    raise BatchError(f"batch '{self.batch_id}' has been detached")
                self._releasing = True

            await self._stop_creation()
            await self._stop_background_tasks()
            await self._close_cached_connections("release")

            try:
                await self._delete_claims()
                # Last, so the reaper can still find the batch if release dies partway.
                await self._client.k8s_helper.delete_batch_lease(self._lease_name, self.namespace)
            finally:
                # The watch is stopped, so nothing else would ever wake a blocked consumer.
                async with self._cond:
                    self._state.close()
                    self._cond.notify_all()

            async with self._lock:
                self._release_done = True
            self._client._unregister_batch(self.namespace, self.batch_id)

    async def _delete_claims(self) -> None:
        """Deletes the batch's claims by label, re-listing until only terminating claims remain,
        since ``deletecollection`` is not atomic.
        """
        label_selector = f"{BATCH_ID_LABEL}={self.batch_id}"
        for round_index in range(BATCH_RELEASE_MAX_DELETE_ROUNDS):
            if round_index:
                await asyncio.sleep(BATCH_RELEASE_RELIST_INTERVAL_SECONDS)
            await self._client.k8s_helper.delete_sandbox_claim_collection(self.namespace, label_selector)
            items, _ = await self._client.k8s_helper.list_sandbox_claim_objects(
                self.namespace, label_selector
            )
            if all((item.get("metadata") or {}).get("deletionTimestamp") for item in items):
                return
        raise BatchError(
            f"batch '{self.batch_id}' still has claims without a deletionTimestamp after "
            f"{BATCH_RELEASE_MAX_DELETE_ROUNDS} deletecollection rounds"
        )

    async def _close_cached_connections(self, reason: str) -> None:
        async with self._lock:
            connected = list(self._connected.values())
            self._connected.clear()
        for sandbox in connected:
            try:
                await sandbox.close_connection()
            except Exception as e:
                logger.warning(
                    f"Batch '{self.batch_id}' failed to close a cached sandbox connection "
                    f"during {reason}: {e}"
                )

    def _start_creation(self, plan: list[tuple[str, BatchGroup]]) -> None:
        self._create_task = asyncio.ensure_future(self._run_creates(plan))

    async def _stop_creation(self) -> None:
        """Cancels the creation task and its in-flight creates.

        Cancelling a worker stops this client from waiting on its POST, not the apiserver from
        having accepted it, so a create can still land after ``release()``'s ``deletecollection``.
        Such a claim is caught by the re-list rounds, or failing that by its ``shutdownTime``.
        """
        task = self._create_task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._create_task = None

    async def _run_creates(self, plan: list[tuple[str, BatchGroup]]) -> None:
        """Paced producer: starts at most ``create_rps`` creates per second, with at most
        ``max_in_flight`` running at once.
        """
        workers: set[asyncio.Task] = set()
        try:
            for claim_name, group in plan:
                # Take a slot before pacing, so a start is never scheduled for a moment it
                # would then spend waiting for a slot.
                await self._in_flight_slots.acquire()
                if await self._skip_if_pool_cancelled(claim_name, group):
                    continue
                delay = self._pacer.reserve(self._clock()) - self._clock()
                if delay > 0:
                    try:
                        await self._sleep(delay)
                    except BaseException:
                        self._in_flight_slots.release()
                        raise
                if await self._skip_if_pool_cancelled(claim_name, group):
                    continue
                worker = asyncio.ensure_future(self._create_one(claim_name, group))
                workers.add(worker)
                worker.add_done_callback(workers.discard)
            if workers:
                await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)

    def _cancel_remaining_creates(self, pools: list[str]) -> None:
        """Marks pools whose remaining creates the producer should skip (per-group fail-fast)."""
        for pool in pools:
            if pool not in self._cancelled_pools:
                self._cancelled_pools.add(pool)
                logger.info(
                    f"Batch '{self.batch_id}' group '{pool}' can no longer reach min_ready; "
                    f"cancelling its remaining creates"
                )

    async def _skip_if_pool_cancelled(self, claim_name: str, group: BatchGroup) -> bool:
        async with self._cond:
            if group.warmpool not in self._cancelled_pools:
                return False
            self._state.mark_create_cancelled(claim_name)
            self._cond.notify_all()
        self._in_flight_slots.release()
        return True

    async def _create_one(self, claim_name: str, group: BatchGroup) -> None:
        try:
            error = await self._create_with_retry(claim_name, group)
            if error is None:
                return
            logger.debug(f"Batch '{self.batch_id}' failed to create claim '{claim_name}': {error}")
            async with self._cond:
                if self._state.mark_create_failed(claim_name, str(error)):
                    self._cancel_remaining_creates([group.warmpool])
                self._cond.notify_all()
        finally:
            self._in_flight_slots.release()

    async def _create_with_retry(self, claim_name: str, group: BatchGroup) -> Exception | None:
        """Creates one claim, retrying 429/5xx per the batch create-retry contract.

        Returns ``None`` once the claim exists, or the error that made the create fail.
        """
        annotations = {
            **self._claim_annotations,
            BATCH_GROUP_SIZE_ANNOTATION: str(group.size),
            BATCH_GROUP_MIN_READY_ANNOTATION: str(group.min_ready),
        }
        attempt = 0
        error: Exception
        while True:
            attempt += 1
            # Computed per request: shutdownTime is relative to this claim's own create time.
            lifecycle = construct_sandbox_claim_lifecycle_spec(
                self._quorum_timeout + self._work_budget + BATCH_DEFAULT_SHUTDOWN_MARGIN_SECONDS
            )
            try:
                await self._client.k8s_helper.create_sandbox_claim(
                    claim_name,
                    group.warmpool,
                    self.namespace,
                    annotations=annotations,
                    labels=self._claim_labels,
                    lifecycle=lifecycle,
                )
                return None
            except asyncio.CancelledError:
                raise
            except ApiException as e:
                status, headers, error = e.status, e.headers, e
            except aiohttp.ClientSSLError as e:
                # A subclass of ClientConnectionError, but not known to be transient.
                return e
            except _CREATE_TRANSPORT_ERRORS as e:
                status, headers, error = None, None, e
            except Exception as e:
                return e
            outcome = batch_state.classify_create_error(status, attempt)
            if outcome is batch_state.CreateOutcome.SUCCESS:
                return None
            if outcome is batch_state.CreateOutcome.FAIL:
                return error
            await self._sleep(
                batch_state.create_backoff_delay(attempt, batch_state.parse_retry_after(headers))
            )

    async def _watch_loop(self) -> None:
        """Background watch loop which keeps ``self._state`` in sync with the batch's claims.

        kubernetes_asyncio's informer/reflector helpers don't support custom resources, so
        this manually implements one where the batch state is seeded from a list,
        then kept up to date with watch events using the list's resourceVersion so no events are missed.
        """
        rv = self._list_resource_version
        label_selector = f"{BATCH_ID_LABEL}={self.batch_id}"
        while True:
            try:
                async for event in self._client.k8s_helper.watch_sandbox_claims(
                    self.namespace, label_selector, rv, _WATCH_TIMEOUT_SECONDS
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
                            self._state.mark_lost((obj.get("metadata") or {}).get("name", ""))
                        elif event_type in ("ADDED", "MODIFIED"):
                            self._state.upsert_claim(obj)
                        self._cond.notify_all()
            except asyncio.CancelledError:
                raise
            except client.ApiException as e:
                # Indicates etcd RV compaction aged our resourceVersoin out of the apiserver's watch cache,
                # so the watch can't continue. Re-list, reconcile the cache (catches events that occurred while there
                # was no watch), and continue watching from the new list's resourceVersion.
                if e.status == 410:
                    while True:
                        try:
                            items, rv = await self._client.k8s_helper.list_sandbox_claim_objects(
                                self.namespace, label_selector
                            )
                        except client.ApiException as list_exc:
                            if list_exc.status == 429 or (
                                list_exc.status is not None and 500 <= list_exc.status < 600
                            ):
                                # Retry as the error is likely transient
                                await asyncio.sleep(0.5)
                                continue
                            async with self._lock:
                                self._state.note_error(list_exc)
                            return
                        else:
                            break
                    async with self._lock:
                        self._state.resync_from_list(items)
                        self._cond.notify_all()
                    continue
                if e.status == 429 or (e.status is not None and 500 <= e.status < 600):
                    # Likely transient: retry from the last-seen resourceVersion.
                    await asyncio.sleep(0.5)
                    continue
                # Response codes outside of this (e.g. 403, 404) are not recoverable
                # by retrying, so we error then exit
                async with self._lock:
                    self._state.note_error(e)
                return
            except aiohttp.ClientSSLError as e:
                # aiohttp.ClientSSLError is a subclass of aiohttp.ClientConnectionError, so it
                # must be caught ahead of the tuple below. It is not known to be transient
                # so surface it via err() instead of retrying.
                async with self._lock:
                    self._state.note_error(e)
                return
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                ConnectionError,
            ):
                # Likely transient network-level failure so retry
                await asyncio.sleep(0.5)
                continue
            except Exception as e:
                # Anything else is not known to be transient either, so surface it via err().
                async with self._lock:
                    self._state.note_error(e)
                return

    async def _renew_loop(self) -> None:
        """Background lease renewal loop: renews at roughly 1/3 of lease duration
        so a couple of missed renewals in a row still leave margin before the Lease goes stale.
        """
        interval = max(1, self._lease_duration // 3)
        while True:
            await asyncio.sleep(interval)
            if not await self._renew_once():
                return

    async def _renew_once(self) -> bool:
        """Does a single attempt to renew a Lease. A transient failure sets ``_renewal_degraded``
        and logs, and if the Lease has not been successfully renewed for a full Lease duration,
        sets a batch-level error.

        Returns ``False`` if another holder has taken the Lease or the Lease no longer exists,
        meaning this handle must stop renewing rather than overwrite it.
        """
        try:
            lease = await self._client.k8s_helper.read_batch_lease(self._lease_name, self.namespace)
            if lease is None:
                async with self._lock:
                    self._state.note_error(
                        BatchLeaseExpiredError(
                            f"batch '{self.batch_id}' Lease '{self._lease_name}' no longer exists"
                        )
                    )
                return False
            if lease.spec.holder_identity != self._holder_identity:
                async with self._lock:
                    self._state.note_error(
                        BatchInUseError(
                            f"batch '{self.batch_id}' Lease is no longer held by this handle "
                            f"(current holder: {lease.spec.holder_identity!r})"
                        )
                    )
                return False
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
                    self._state.note_lease_degraded()
                    self._cond.notify_all()
                # time.monotonic() is a clock that only ever moves forward, so unlike datetime.now(),
                # it can't jump backward from an NTP correction or a system clock change.
                if time.monotonic() - self._last_renew_success >= self._lease_duration:
                    self._state.note_error(
                        BatchLeaseExpiredError(
                            f"batch '{self.batch_id}' lease has not renewed successfully "
                            f"for {self._lease_duration}s"
                        )
                    )
            return True
        async with self._lock:
            self._last_renew_success = time.monotonic()
            self._renewal_degraded = False
        return True
