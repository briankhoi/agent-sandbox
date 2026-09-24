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
Async version of :class:`SandboxClient` for use in async applications.

Requires the ``async`` optional dependencies::

    pip install k8s-agent-sandbox[async]
"""

import atexit
import asyncio
import logging
import sys
import uuid
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Generic, TypeVar

from .async_k8s_helper import AsyncK8sHelper
from .async_sandbox import AsyncSandbox
from .async_sandbox_batch import AsyncSandboxBatch
from .exceptions import SandboxNotFoundError
from .k8s_helper import K8sHelper
from .pod_metadata import build_pod_metadata, validate_labels
from .utils import construct_sandbox_claim_lifecycle_spec
from .constants import BATCH_ID_LABEL
from .models import BatchGroup, SandboxConnectionConfig, SandboxTracerConfig
from .trace_manager import async_trace_span, create_tracer_manager, initialize_tracer, trace

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=AsyncSandbox)

# Bounds each per-claim delete (and each batch deletecollection and Lease delete)
# issued by the atexit cleanup below. urllib3
# (used by the synchronous K8sHelper) has no default read timeout, so an
# unresponsive apiserver would otherwise hang process exit indefinitely.
_ATEXIT_DELETE_REQUEST_TIMEOUT_SECONDS = 300


class AsyncSandboxClient(Generic[T]):
    """
    Async registry-based client for managing Sandbox lifecycles.

    Use as an async context manager for automatic cleanup::

        async with AsyncSandboxClient(connection_config=config) as client:
            sandbox = await client.create_sandbox("python-sandbox-pool")
            result = await sandbox.commands.run("echo hello")

    ``connection_config`` is required — the async client does not support
    ``SandboxLocalTunnelConnectionConfig``.

    By default (``cleanup=True``) an atexit hook is registered that deletes
    all tracked sandboxes on program termination, so sandboxes are not leaked
    if the program exits without explicit cleanup. The hook also terminates
    loop-independent local resources such as sandboxd port-forward processes.
    Pass ``cleanup=False`` to opt out of this behavior::

        client = AsyncSandboxClient(connection_config=config, cleanup=False)

    Note that this default differs from the synchronous ``SandboxClient``,
    which defaults to ``cleanup=False``; the async client opts in to safer
    out-of-the-box cleanup.

    Alternatively, use the ``async with`` context manager or explicitly call
    ``await client.delete_all()`` followed by ``await client.close()`` to
    avoid orphaned claims.
    """

    sandbox_class: type[T] = AsyncSandbox  # type: ignore

    def __init__(
        self,
        connection_config: SandboxConnectionConfig | None = None,
        tracer_config: SandboxTracerConfig | None = None,
        cleanup: bool = True,
    ) -> None:
        """
        Args:
            connection_config: Configuration for connecting to the sandboxes.
                Required — the async client does not support
                ``SandboxLocalTunnelConnectionConfig``.
            tracer_config: Configuration for OpenTelemetry tracing.
                Defaults to an empty SandboxTracerConfig (tracing disabled).
            cleanup: If True, registers an atexit hook to automatically delete
                all tracked sandboxes when the program terminates. The hook
                synchronously terminates loop-independent local resources and
                uses the synchronous ``K8sHelper`` for claim deletion, so it
                remains usable during interpreter shutdown. Cleanup is
                best-effort — per-claim and top-level failures emit warnings to
                ``sys.stderr`` rather than raising. Defaults to True so that
                sandboxes are not leaked when a caller forgets to clean up;
                pass ``cleanup=False`` to opt out. Note this differs from the
                synchronous ``SandboxClient``, which defaults to False.
        """
        if connection_config is None:
            raise ValueError(
                "connection_config is required for AsyncSandboxClient. "
                "Use SandboxDirectConnectionConfig, SandboxGatewayConnectionConfig, "
                "SandboxInClusterConnectionConfig, or SandboxdPodTunnelConnectionConfig. "
                "For local development with the router's port-forward, use the synchronous SandboxClient; "
                "SandboxdPodTunnelConnectionConfig supports async pod port-forwarding."
            )

        self.connection_config = connection_config

        self.tracer_config = tracer_config or SandboxTracerConfig()
        if self.tracer_config.enable_tracing:
            initialize_tracer(self.tracer_config.trace_service_name)
        self.tracing_manager, self.tracer = create_tracer_manager(self.tracer_config)

        self.k8s_helper = AsyncK8sHelper()

        self._active_connection_sandboxes: dict[tuple[str, str], T] = {}
        self._active_batches: dict[tuple[str, str], AsyncSandboxBatch] = {}
        self._lock = asyncio.Lock()

        if cleanup:
            atexit.register(self._atexit_cleanup)

    async def __aenter__(self) -> "AsyncSandboxClient[T]":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        try:
            await self.delete_all()
        finally:
            await self.close()

    async def close(self) -> None:
        """Shuts down tracked sandbox connections and the K8s API client.

        A connection that fails to close remains tracked so a later call can
        retry its cleanup.
        """
        async with self._lock:
            for key, sandbox in list(self._active_connection_sandboxes.items()):
                try:
                    await sandbox.close_connection()
                except Exception as e:
                    logger.error(f"Failed to close sandbox connection: {e}")
                else:
                    self._active_connection_sandboxes.pop(key, None)
            batches = list(self._active_batches.values())
            self._active_batches.clear()
        for batch in batches:
            try:
                await batch._stop_creation()
                await batch._stop_background_tasks()
            except Exception as e:
                logger.error(f"Failed to stop batch '{batch.batch_id}' tasks: {e}")
        await self.k8s_helper.close()

    async def create_sandbox(
        self,
        warmpool: str,
        namespace: str = "default",
        sandbox_ready_timeout: int = 180,
        labels: dict[str, str] | None = None,
        *,
        shutdown_after_seconds: int | None = None,
        volume_claim_templates: list[dict] | None = None,
        pod_labels: dict[str, str] | None = None,
        pod_annotations: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
    ) -> T:
        """Provisions a new Sandbox claim and returns an async Sandbox handle.

        Args:
            warmpool: Name of the SandboxWarmPool to use.
            namespace: Kubernetes namespace for the claim.
            sandbox_ready_timeout: Seconds to wait for the sandbox to be ready.
            labels: Optional Kubernetes labels to attach to the claim object
                (``SandboxClaim.metadata.labels``).
            shutdown_after_seconds: Optional TTL in seconds. When set, the
                claim's ``spec.lifecycle`` is populated with a ``shutdownTime``
                of *now + shutdown_after_seconds* (UTC) and a ``shutdownPolicy``
                of ``"Delete"``, so the controller auto-deletes the claim on
                expiry. Must be a positive integer.
            volume_claim_templates: Optional list of volume claim templates
                to override/merge with the sandbox template.
            pod_labels: Optional labels stamped onto the running Sandbox **Pod**
                via ``spec.additionalPodMetadata.labels``. Unlike ``labels``
                (which land on the claim object), these are readable from inside
                the sandbox through the Downward API.
            pod_annotations: Optional annotations stamped onto the running
                Sandbox **Pod** via ``spec.additionalPodMetadata.annotations``.
            env: Optional environment variables to inject into the SandboxClaim.
                Setting this populates ``spec.env`` and forces a cold start
                from the warm pool template instead of adopting a pre-warmed
                pod, which may increase startup latency.

        Example::

            async with AsyncSandboxClient(connection_config=config) as client:
                sandbox = await client.create_sandbox("python-sandbox-pool")
                result = await sandbox.commands.run("echo 'Hello'")
        """
        if not warmpool:
            raise ValueError("Warmpool name cannot be empty.")

        if labels:
            validate_labels(labels)

        pod_metadata = build_pod_metadata(pod_labels, pod_annotations)

        lifecycle = construct_sandbox_claim_lifecycle_spec(shutdown_after_seconds) if shutdown_after_seconds is not None else None

        claim_name = f"sandbox-claim-{uuid.uuid4().hex[:8]}"

        try:
            created_claim = await self._create_claim(
                claim_name,
                warmpool,
                namespace,
                labels=labels,
                lifecycle=lifecycle,
                volume_claim_templates=volume_claim_templates,
                pod_metadata=pod_metadata,
                env=env,
            )
            # Wait for the claim to be bound and Ready in a single watch.
            # The claim status carries the sandbox name (which differs from
            # the claim name with warm pools) and the forwarded Ready
            # condition in the same status update, so no second watch on the
            # Sandbox resource is needed. The watch starts from the create
            # response's resourceVersion so the apiserver serves it from the
            # watch cache instead of a quorum etcd read per wait.
            claim_rv = None
            if isinstance(created_claim, dict):
                claim_rv = (created_claim.get("metadata") or {}).get("resourceVersion")
            sandbox_id = await self._wait_for_claim_ready(
                claim_name, namespace, sandbox_ready_timeout, resource_version=claim_rv
            )

            sandbox = self.sandbox_class(
                claim_name=claim_name,
                sandbox_id=sandbox_id,
                namespace=namespace,
                connection_config=self.connection_config,
                tracer_config=self.tracer_config,
                k8s_helper=self.k8s_helper,
            )
        except (Exception, asyncio.CancelledError):
            await asyncio.shield(self._delete_claim(claim_name, namespace))
            raise

        async with self._lock:
            self._active_connection_sandboxes[(namespace, claim_name)] = sandbox
        return sandbox

    async def get_sandbox(
        self,
        claim_name: str,
        namespace: str = "default",
        resolve_timeout: int = 30,
        warmpool_name: str | None = None,
    ) -> T:
        """Retrieves an existing sandbox handle given a sandbox claim name.

        Args:
            claim_name: Name of the SandboxClaim to attach to.
            namespace: Kubernetes namespace the claim lives in.
            resolve_timeout: Seconds to wait while resolving the sandbox
                name from the claim status.
            warmpool_name: Optional SandboxWarmPool name to validate against
                the existing claim's ``spec.warmPoolRef.name``.
                When supplied and the claim references a different
                warmpool, ``ValueError`` is raised before returning a
                handle. Mirrors the sync ``SandboxClient.get_sandbox``
                guard so async session-reattach callers get the same
                refuse-on-mismatch semantics.

        Example::

            sandbox = await client.get_sandbox("sandbox-claim-1234abcd")
            result = await sandbox.commands.run("ls -la")
        """
        key = (namespace, claim_name)

        async with self._lock:
            existing = self._active_connection_sandboxes.get(key)

        try:
            if warmpool_name is not None:
                claim_object = await self.k8s_helper.get_sandbox_claim(
                    claim_name, namespace
                )
                if not claim_object:
                    raise SandboxNotFoundError(
                        f"SandboxClaim '{claim_name}' not found in namespace '{namespace}'."
                    )
                existing_warmpool = (
                    claim_object.get("spec", {})
                    .get("warmPoolRef", {})
                    .get("name")
                )
                if existing_warmpool != warmpool_name:
                    raise ValueError(
                        f"SandboxClaim '{claim_name}' in namespace '{namespace}' references "
                        f"warmpool '{existing_warmpool}', not '{warmpool_name}'. Refusing "
                        f"to reattach."
                    )
            sandbox_id = await self.k8s_helper.resolve_sandbox_name(
                claim_name, namespace, timeout=resolve_timeout
            )
            sandbox_object = await self.k8s_helper.get_sandbox(sandbox_id, namespace)
            if not sandbox_object:
                raise SandboxNotFoundError(f"Underlying Sandbox '{sandbox_id}' not found.")
        except ValueError:
            # Warmpool mismatch is a signed-off refusal — propagate
            # untouched so the caller sees the security-relevant reason
            # rather than a generic "not found" wrap.
            raise
        except Exception as e:
            if existing:
                await existing.terminate()
            async with self._lock:
                self._active_connection_sandboxes.pop(key, None)
            raise SandboxNotFoundError(
                f"Sandbox claim '{claim_name}' not found or resolution failed "
                f"in namespace '{namespace}': {e}"
            ) from e

        if existing and existing.is_active:
            return existing

        if existing:
            async with self._lock:
                self._active_connection_sandboxes.pop(key, None)

        new_handle = self.sandbox_class(
            claim_name=claim_name,
            sandbox_id=sandbox_id,
            namespace=namespace,
            connection_config=self.connection_config,
            tracer_config=self.tracer_config,
            k8s_helper=self.k8s_helper,
        )

        async with self._lock:
            self._active_connection_sandboxes[key] = new_handle
        return new_handle

    async def list_active_sandboxes(self) -> list[tuple[str, str]]:
        """Returns a list of ``(namespace, claim_name)`` tuples currently managed."""
        async with self._lock:
            for key, obj in list(self._active_connection_sandboxes.items()):
                if not obj.is_active:
                    self._active_connection_sandboxes.pop(key, None)
            return list(self._active_connection_sandboxes.keys())

    async def list_all_sandboxes(self, namespace: str = "default", label_selector: str | None = None) -> list[str]:
        """Lists all SandboxClaim names in the Kubernetes cluster for a namespace.

        Args:
            namespace: Kubernetes namespace to list claims in.
            label_selector: Optional Kubernetes label selector string
                (e.g. ``"app=myapp"``). When set, only claims matching
                the selector are returned.
        """
        return await self.k8s_helper.list_sandbox_claims(namespace, label_selector=label_selector)

    @async_trace_span("claim_batch")
    async def claim_batch(
        self,
        groups: Sequence[BatchGroup],
        *,
        namespace: str = "default",
        labels: Mapping[str, str] | None = None,
        batch_id: str | None = None,
        create_rps: float | None = None,
        max_in_flight: int | None = None,
        work_budget: int | None = None,
        quorum_timeout: int | None = None,
        lease_duration: int | None = None,
    ) -> AsyncSandboxBatch:
        """Creates a batch of SandboxClaims across one or more warm pools and returns its handle.

        Checks that each group's SandboxWarmPool and its SandboxTemplate exist, creates the
        batch's ``coordination.k8s.io/v1`` Lease (``batch-<id>``), starts the label-scoped watch
        and Lease renewal, then creates the claims ``<id>-0`` to ``<id>-<N-1>`` in the background
        and returns without waiting for them. Consume the members with ``events()`` or
        ``iter_ready_groups()`` and tear the batch down with ``release()``.

        Each claim is deleted by the controller at its ``shutdownTime``, its own create time plus
        ``quorum_timeout + work_budget`` and a 600 s margin, as a backstop if ``release()`` never runs.

        Args:
            groups: One ``BatchGroup`` per warm pool, each with ``size > 0``; pools must be distinct.
            namespace: Namespace for the claims and the Lease.
            labels: Extra labels for every claim; must not set ``agents.x-k8s.io/batch-id``.
            batch_id: Overrides the generated id; a DNS-1123 label starting with a letter,
                at most 52 characters.
            create_rps: Maximum claim creates started per second (default 50).
            max_in_flight: Maximum concurrent claim creates (default 20).
            work_budget: Expected working time after quorum, in seconds (int, default 3600).
            quorum_timeout: Seconds each group has to reach ``min_ready`` (int, default 600).
            lease_duration: Lease duration in seconds (int, default 60); must exceed 5.

        Raises:
            ValueError: an invalid argument, before anything is created.
            SandboxWarmPoolNotFoundError: a group's warm pool does not exist.
            SandboxTemplateNotFoundError: a group's warm pool names a template that does not exist.
            BatchExistsError: the batch's Lease already exists.

        Example::

            batch = await client.claim_batch([BatchGroup(warmpool="python-sandbox-pool", size=3)])
            try:
                async for event in batch.events():
                    if event.type is BatchEventType.MEMBER_READY:
                        sandbox = await batch.connect(event.member)
                        await sandbox.commands.run("echo hello")
            finally:
                await batch.release()
        """
        annotations = self._trace_context_annotations()

        batch = await AsyncSandboxBatch._claim(
            self,
            groups,
            namespace=namespace,
            labels=labels,
            batch_id=batch_id,
            create_rps=create_rps,
            max_in_flight=max_in_flight,
            work_budget=work_budget,
            quorum_timeout=quorum_timeout,
            lease_duration=lease_duration,
            claim_annotations=annotations,
        )
        async with self._lock:
            self._active_batches[(namespace, batch.batch_id)] = batch
        return batch

    async def get_batch(self, batch_id: str, namespace: str = "default") -> AsyncSandboxBatch:
        """Attaches to an existing batch, taking over its Lease.

        Resumes batch lease renewal and starts a label-scoped watch that keeps
        ``members()`` up to date. Returns an error if the lease has holderIdentity set.

        Example::

            batch = await client.get_batch("b1234abcd12")
            ready = [m for m in batch.members() if m.ready]
        """
        key = (namespace, batch_id)
        batch = await AsyncSandboxBatch._attach(self, batch_id, namespace)
        async with self._lock:
            self._active_batches[key] = batch
        return batch

    def _unregister_batch(self, namespace: str, batch_id: str) -> None:
        self._active_batches.pop((namespace, batch_id), None)

    async def delete_sandbox(self, claim_name: str, namespace: str = "default") -> None:
        """Stops the client side connection and deletes the Kubernetes resources."""
        key = (namespace, claim_name)
        async with self._lock:
            sandbox = self._active_connection_sandboxes.get(key)
        try:
            if sandbox:
                await sandbox.terminate()
                async with self._lock:
                    self._active_connection_sandboxes.pop(key, None)
            else:
                await self._delete_claim(claim_name, namespace)
        except Exception as e:
            logger.error(
                f"Failed to delete sandbox '{claim_name}' in namespace '{namespace}': {e}"
            )

    async def delete_all(self) -> None:
        """Cleanup all tracked sandboxes managed by this client, and release its tracked batches.
        Detached batches are no longer tracked, so they are left alone.
        """
        async with self._lock:
            items = list(self._active_connection_sandboxes.items())
            batches = list(self._active_batches.items())

        for (ns, claim_name), _ in items:
            try:
                await self.delete_sandbox(claim_name, namespace=ns)
            except Exception as e:
                logger.error(f"Cleanup failed for {claim_name} in namespace {ns}: {e}")
        for (ns, batch_id), batch in batches:
            if batch._detached:
                continue
            try:
                await batch.release()
            except Exception as e:
                logger.error(f"Cleanup failed for batch {batch_id} in namespace {ns}: {e}")

    def _atexit_cleanup(self):
        """Best-effort atexit cleanup for claims and local sandbox resources.

        Tracked sandbox handles use their synchronous, loop-independent
        emergency path first so sandboxd port-forward processes are not left
        behind. Claim deletion uses the synchronous :class:`K8sHelper` because
        an atexit handler may run after async event-loop resources and the
        process-wide executor have started shutting down. Per-claim failures
        and top-level errors are reported to ``sys.stderr`` rather than raised.

        Tracked batches are released the same way: one ``deletecollection`` on the batch label,
        then the Lease delete. Their asyncio tasks are left alone, since the event loop may be gone.
        """
        try:
            claims = list(self._active_connection_sandboxes.keys())
            tracked = [
                (key, self._active_connection_sandboxes[key]) for key in claims
            ]
            batches = [
                batch for batch in self._active_batches.values() if not batch._detached
            ]
            if not tracked and not batches:
                return

            for _, sandbox in tracked:
                try:
                    sandbox._close_for_atexit()
                except Exception as e:
                    if sys.stderr is not None:
                        print(
                            "[agent-sandbox] Warning: failed to close sandbox "
                            f"connection during atexit cleanup: {e}",
                            file=sys.stderr,
                        )

            helper = K8sHelper()
            for batch in batches:
                try:
                    helper.delete_sandbox_claim_collection(
                        batch.namespace,
                        f"{BATCH_ID_LABEL}={batch.batch_id}",
                        _request_timeout=_ATEXIT_DELETE_REQUEST_TIMEOUT_SECONDS,
                    )
                    helper.delete_batch_lease(
                        batch._lease_name,
                        batch.namespace,
                        _request_timeout=_ATEXIT_DELETE_REQUEST_TIMEOUT_SECONDS,
                    )
                except Exception as e:
                    if sys.stderr is not None:
                        print(
                            f"[agent-sandbox] Warning: failed to release batch "
                            f"'{batch.batch_id}' in namespace '{batch.namespace}' during atexit cleanup: {e}",
                            file=sys.stderr,
                        )
            for ns, claim_name in (key for key, _ in tracked):
                try:
                    helper.delete_sandbox_claim(
                        claim_name,
                        ns,
                        _request_timeout=_ATEXIT_DELETE_REQUEST_TIMEOUT_SECONDS,
                    )
                except Exception as e:
                    if sys.stderr is not None:
                        print(
                            f"[agent-sandbox] Warning: failed to delete sandbox claim "
                            f"'{claim_name}' in namespace '{ns}' during atexit cleanup: {e}",
                            file=sys.stderr,
                        )
        except Exception as e:
            if sys.stderr is not None:
                print(
                    f"[agent-sandbox] Warning: atexit cleanup failed: {e}",
                    file=sys.stderr,
                )

    def _trace_context_annotations(self) -> dict[str, str]:
        """The current trace context as a claim annotation, when tracing is enabled."""
        annotations = {}
        if self.tracing_manager:
            trace_context_str = self.tracing_manager.get_trace_context_json()
            if trace_context_str:
                annotations["opentelemetry.io/trace-context"] = trace_context_str
        return annotations

    @async_trace_span("create_claim")
    async def _create_claim(
        self,
        claim_name: str,
        warmpool_name: str,
        namespace: str,
        labels: dict[str, str] | None = None,
        lifecycle: dict | None = None,
        volume_claim_templates: list[dict] | None = None,
        pod_metadata: dict | None = None,
        env: dict[str, str] | None = None,
    ):
        """Create a claim with lifecycle, metadata, and trace context."""
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("sandbox.claim.name", claim_name)
            if lifecycle:
                span.set_attribute("sandbox.lifecycle.shutdown_time", lifecycle["shutdownTime"])
                span.set_attribute("sandbox.lifecycle.shutdown_policy", lifecycle["shutdownPolicy"])

        annotations = self._trace_context_annotations()

        return await self.k8s_helper.create_sandbox_claim(
            claim_name,
            warmpool_name,
            namespace,
            annotations=annotations,
            labels=labels,
            lifecycle=lifecycle,
            volume_claim_templates=volume_claim_templates,
            pod_metadata=pod_metadata,
            env=env,
        )

    @async_trace_span("wait_for_claim_ready")
    async def _wait_for_claim_ready(self, claim_name: str, namespace: str, timeout: int, resource_version: str | None = None) -> str:
        """Waits for the SandboxClaim to be bound and Ready, returning the sandbox name."""
        return await self.k8s_helper.wait_for_claim_ready(claim_name, namespace, timeout, resource_version=resource_version)

    @async_trace_span("wait_for_sandbox_ready")
    async def _wait_for_sandbox_ready(
        self, sandbox_id: str, namespace: str, timeout: int
    ) -> None:
        """Waits for the Sandbox custom resource to have a 'Ready' status."""
        await self.k8s_helper.wait_for_sandbox_ready(sandbox_id, namespace, timeout)

    @async_trace_span("delete_claim")
    async def _delete_claim(self, claim_name: str, namespace: str) -> None:
        """Delete a claim through the client's shared Kubernetes helper."""
        await self.k8s_helper.delete_sandbox_claim(claim_name, namespace)
