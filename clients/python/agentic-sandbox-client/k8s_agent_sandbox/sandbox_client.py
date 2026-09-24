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
This module provides the SandboxClient for interacting with the Agentic Sandbox.
It handles lifecycle management (claiming, waiting) and interaction (execution,
file I/O) via the Sandbox resource handle.
"""

import uuid
import atexit
import sys
import logging
from collections.abc import Mapping, Sequence
from typing import List, Dict, Tuple, TypeVar, Generic, Type

# Import all tracing components from the trace_manager module
from .trace_manager import (
    create_tracer_manager, initialize_tracer, trace_span, trace
)
from .sandbox import Sandbox
from .sandbox_batch import SandboxBatch
from .models import (
    BatchGroup,
    SandboxConnectionConfig,
    SandboxLocalTunnelConnectionConfig,
    SandboxTracerConfig,
)
from .k8s_helper import K8sHelper
from .pod_metadata import build_pod_metadata, validate_labels
from .utils import construct_sandbox_claim_lifecycle_spec
from .exceptions import SandboxNotFoundError

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    stream=sys.stdout)

T = TypeVar('T', bound=Sandbox)

class SandboxClient(Generic[T]):
    """
    A registry-based client for managing Sandbox lifecycles.
    Tracks all active handles to ensure flat code structure and safe cleanup.
    """

    sandbox_class: Type[T] = Sandbox  # type: ignore

    def __init__(
        self,
        connection_config: SandboxConnectionConfig | None = None,
        tracer_config: SandboxTracerConfig | None = None,
        cleanup: bool = False,
    ) -> None:
        """
        Initializes the SandboxClient.

        Args:
            connection_config: Configuration for connecting to the sandboxes. 
                Defaults to SandboxLocalTunnelConnectionConfig() which uses 
                kubectl port-forwarding. Can also be SandboxDirectConnectionConfig 
                or SandboxGatewayConnectionConfig.
            tracer_config: Configuration for OpenTelemetry tracing. 
                Defaults to an empty SandboxTracerConfig (tracing disabled).
            cleanup: If True, registers an atexit hook to automatically delete 
                all tracked sandboxes when the program terminates. Defaults to False.
        """
        # Sandbox related configuration
        self.connection_config = connection_config or SandboxLocalTunnelConnectionConfig()
        
        # Tracer configuration
        self.tracer_config = tracer_config or SandboxTracerConfig()
        if self.tracer_config.enable_tracing:
            initialize_tracer(self.tracer_config.trace_service_name)
        self.tracing_manager, self.tracer = create_tracer_manager(self.tracer_config)

        # Downstream Kubernetes Configuration
        self.k8s_helper = K8sHelper()
        
        # Tracks all the active client side connections to the created sandbox claims
        self._active_connection_sandboxes: Dict[Tuple[str, str], T] = {}

        # Tracks all the active batch handles.
        self._active_batches: Dict[Tuple[str, str], SandboxBatch] = {}

        # Optional automatic cleanup of sandboxes on program termination
        if cleanup:
            atexit.register(self.delete_all)

    def create_sandbox(
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
        """Provisions new Sandbox claim and returns a Sandbox handle which tracks
           the underlying infrastructure.

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

        Example:

            >>> client = SandboxClient()
            >>> sandbox = client.create_sandbox(warmpool="python-sandbox-pool")
            >>> sandbox.commands.run("echo 'Hello World'")
        """
        if not warmpool:
            raise ValueError("Warmpool name cannot be empty.")

        if labels:
            validate_labels(labels)

        pod_metadata = build_pod_metadata(pod_labels, pod_annotations)

        lifecycle = construct_sandbox_claim_lifecycle_spec(shutdown_after_seconds) if shutdown_after_seconds is not None else None

        claim_name = f"sandbox-claim-{uuid.uuid4().hex[:8]}"

        try:
            created_claim = self._create_claim(
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
            sandbox_id = self._wait_for_claim_ready(
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
        except Exception:
            # If creation or waiting fails, ensure we don't leave an orphaned claim
            self._delete_claim(claim_name, namespace)
            raise

        self._active_connection_sandboxes[(namespace, claim_name)] = sandbox
        return sandbox

    def get_sandbox(
        self,
        claim_name: str,
        namespace: str = "default",
        resolve_timeout: int = 30,
    ) -> T:
        """
        Retrieves an existing sandbox handle given a sandbox claim name.
        If the handle is closed or missing, it re-attaches to the infrastructure.

        Args:
            claim_name: Name of the SandboxClaim to attach to.
            namespace: Kubernetes namespace the claim lives in.
            resolve_timeout: Seconds to wait while resolving the sandbox
                name from the claim status.
        Example:

            >>> client = SandboxClient()
            >>> sandbox = client.get_sandbox(
            ...     "sandbox-claim-1234abcd",
            ... )
            >>> sandbox.commands.run("ls -la")
        """
        key = (namespace, claim_name)
        existing = self._active_connection_sandboxes.get(key)

        # Check if the sandbox actually exists in Kubernetes
        try:
            sandbox_id = self.k8s_helper.resolve_sandbox_name(claim_name, namespace, timeout=resolve_timeout)
            sandbox_object = self.k8s_helper.get_sandbox(sandbox_id, namespace)
            if not sandbox_object:
                raise SandboxNotFoundError(f"Underlying Sandbox '{sandbox_id}' not found.")
        except Exception as e:
            if existing:
                existing.terminate()
            self._active_connection_sandboxes.pop(key, None)
            raise SandboxNotFoundError(f"Sandbox claim '{claim_name}' not found or resolution failed in namespace '{namespace}': {e}") from e

        # If it's already in the registry and active (and verified on K8s), return the existing object
        if existing and existing.is_active:
            return existing

        # If the sandbox is not active, pop it out from the tracking list
        if existing:
            self._active_connection_sandboxes.pop(key, None)

        # Re-attach: Create a fresh handle for the existing ID
        new_handle = self.sandbox_class(
            claim_name=claim_name,
            sandbox_id=sandbox_id,
            namespace=namespace,
            connection_config=self.connection_config,
            tracer_config=self.tracer_config,
            k8s_helper=self.k8s_helper
        )

        self._active_connection_sandboxes[key] = new_handle
        return new_handle
    
    def list_active_sandboxes(self) -> List[Tuple[str, str]]:
        """Returns a list of tuples containing (namespace, claim_name) currently managed by this client.
        
        Example:
        
            >>> client = SandboxClient()
            >>> client.create_sandbox("python-sandbox-pool")
            >>> print(client.list_active_sandboxes())
            [('default', 'sandbox-claim-1234abcd')]
        """
        # We only return IDs that are still active/initialized, and clean up inactive ones.
        for key, obj in list(self._active_connection_sandboxes.items()):
            if not obj.is_active:
                self._active_connection_sandboxes.pop(key, None)
        return list(self._active_connection_sandboxes.keys())
      
    def list_all_sandboxes(self, namespace: str = "default", label_selector: str | None = None) -> List[str]:
        """
        Lists all SandboxClaim names currently existing in the Kubernetes cluster
        for the given namespace.

        Args:
            namespace: Kubernetes namespace to list claims in.
            label_selector: Optional Kubernetes label selector string
                (e.g. ``"app=myapp"``). When set, only claims matching
                the selector are returned.

        Example:

            >>> client = SandboxClient()
            >>> print(client.list_all_sandboxes(namespace="default"))
            ['sandbox-claim-1234abcd', 'sandbox-claim-5678efgh']
        """
        return self.k8s_helper.list_sandbox_claims(namespace, label_selector=label_selector)

    @trace_span("claim_batch")
    def claim_batch(
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
    ) -> SandboxBatch:
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

        Example:

            >>> client = SandboxClient()
            >>> batch = client.claim_batch([BatchGroup(warmpool="python-sandbox-pool", size=3)])
            >>> try:
            ...     for event in batch.events():
            ...         if event.type is BatchEventType.MEMBER_READY:
            ...             batch.connect(event.member).commands.run("echo hello")
            ... finally:
            ...     batch.release()
        """
        annotations = self._trace_context_annotations()

        batch = SandboxBatch._claim(
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
        self._active_batches[(namespace, batch.batch_id)] = batch
        return batch

    def get_batch(self, batch_id: str, namespace: str = "default") -> SandboxBatch:
        """Attaches to an existing batch, taking over its Lease.

        Resumes batch lease renewal and starts a label-scoped watch that keeps
        ``members()`` up to date. Returns an error if the lease has holderIdentity set.

        Example:

            >>> client = SandboxClient()
            >>> batch = client.get_batch("b1234abcd12")
            >>> ready = [m for m in batch.members() if m.ready]
        """
        key = (namespace, batch_id)
        batch = SandboxBatch._attach(self, batch_id, namespace)
        self._active_batches[key] = batch
        return batch

    def _unregister_batch(self, namespace: str, batch_id: str) -> None:
        self._active_batches.pop((namespace, batch_id), None)

    def delete_sandbox(self, claim_name: str, namespace: str = "default") -> None:
        """Stops the client side connection and deletes the Kubernetes resources.
        
        Example:
        
            >>> client = SandboxClient()
            >>> sandbox = client.create_sandbox("python-sandbox-pool")
            >>> client.delete_sandbox(sandbox.claim_name)
        """
        key = (namespace, claim_name)
        sandbox = self._active_connection_sandboxes.get(key)
        try:
            if sandbox:
                sandbox.terminate()
                self._active_connection_sandboxes.pop(key, None)
            else:
                self._delete_claim(claim_name, namespace)
        except Exception as e:
            logging.error(f"Failed to delete sandbox '{claim_name}' in namespace '{namespace}': {e}")
            
    def delete_all(self) -> None:
        """
        Cleanup all tracked sandboxes managed by this client, and release its tracked batches.
        Detached batches are no longer tracked, so they are left alone.
        
        Example:
        
            >>> client = SandboxClient()
            >>> client.create_sandbox("python-sandbox-pool")
            >>> client.create_sandbox("python-sandbox-pool")
            >>> client.delete_all()
        """
        for (ns, claim_name), _ in list(self._active_connection_sandboxes.items()):
            try:
                self.delete_sandbox(claim_name, namespace=ns)
            except Exception as e:
                logging.error(
                    f"Cleanup failed for {claim_name} in namespace {ns}: {e}"
                )
        for (ns, batch_id), batch in list(self._active_batches.items()):
            if batch._detached:
                continue
            try:
                batch.release()
            except Exception as e:
                logging.error(
                    f"Cleanup failed for batch {batch_id} in namespace {ns}: {e}"
                )

    def _trace_context_annotations(self) -> dict[str, str]:
        """The current trace context as a claim annotation, when tracing is enabled."""
        annotations = {}
        if self.tracing_manager:
            trace_context_str = self.tracing_manager.get_trace_context_json()
            if trace_context_str:
                annotations["opentelemetry.io/trace-context"] = trace_context_str
        return annotations

    @trace_span("create_claim")
    def _create_claim(
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
        """Creates the SandboxClaim custom resource in the Kubernetes cluster."""
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("sandbox.claim.name", claim_name)
            if lifecycle:
                span.set_attribute("sandbox.lifecycle.shutdown_time", lifecycle["shutdownTime"])
                span.set_attribute("sandbox.lifecycle.shutdown_policy", lifecycle["shutdownPolicy"])

        annotations = self._trace_context_annotations()

        return self.k8s_helper.create_sandbox_claim(
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

    @trace_span("wait_for_claim_ready")
    def _wait_for_claim_ready(self, claim_name: str, namespace: str, timeout: int, resource_version: str | None = None) -> str:
        """Waits for the SandboxClaim to be bound and Ready, returning the sandbox name."""
        return self.k8s_helper.wait_for_claim_ready(claim_name, namespace, timeout, resource_version=resource_version)

    @trace_span("wait_for_sandbox_ready")
    def _wait_for_sandbox_ready(
        self, sandbox_id: str, namespace: str, timeout: int
    ) -> None:
        self.k8s_helper.wait_for_sandbox_ready(sandbox_id, namespace, timeout)

    @trace_span("delete_claim")
    def _delete_claim(self, claim_name: str, namespace: str) -> None:
        """Deletes the SandboxClaim custom resource from the Kubernetes cluster."""
        self.k8s_helper.delete_sandbox_claim(claim_name, namespace)

    def get_sandbox_claim_warmpool_name(self, claim_name: str, namespace: str) -> str:
        """Get warmpool name of a sandbox claim."""
        claim_object = self.k8s_helper.get_sandbox_claim(claim_name, namespace)
        if not claim_object:
            raise SandboxNotFoundError(
                f"SandboxClaim '{claim_name}' not found in namespace '{namespace}'."
            )
        warmpool_name = (
            claim_object.get("spec", {})
            .get("warmPoolRef", {})
            .get("name")
        )
        return warmpool_name
