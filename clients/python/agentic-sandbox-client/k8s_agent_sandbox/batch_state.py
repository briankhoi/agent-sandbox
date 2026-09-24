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
Batch state core shared by both sync and async batch handles.

Contains methods for modifying and re-populating a batch's state that operate on already-fetched
Kubernetes objects and thus do not perform any network I/O, threading, or locking of their own.
"""

import os
import re
import socket
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from operator import itemgetter

from .constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    TERMINAL_CLAIM_READY_REASONS,
)
from .exceptions import BatchError
from .models import BatchGroup, Member

CLOCK_SKEW_MARGIN = 5
BATCH_DEFAULT_LEASE_DURATION_SECONDS = 60
BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS = 600
BATCH_DEFAULT_WORK_BUDGET_SECONDS = 3600
BATCH_DEFAULT_SHUTDOWN_MARGIN_SECONDS = 600
BATCH_DEFAULT_CREATE_RPS = 50.0
BATCH_DEFAULT_MAX_IN_FLIGHT = 20
BATCH_CREATE_MAX_ATTEMPTS = 3
# Jittered exponential backoff between create attempts on 429/5xx, used when the
# response carries no Retry-After header. Retry-After is honored up to our set max.
BATCH_CREATE_BACKOFF_BASE_SECONDS = 0.5
BATCH_CREATE_BACKOFF_MAX_SECONDS = 10.0
BATCH_CREATE_RETRY_AFTER_MAX_SECONDS = 60.0
# release() re-lists after each deletecollection until only terminating claims remain,
# since deletecollection is not atomic and an in-flight create can land after it.
BATCH_RELEASE_MAX_DELETE_ROUNDS = 10
BATCH_RELEASE_RELIST_INTERVAL_SECONDS = 0.5
# Upper bound on how long the sync handle's release() and detach() each wait for creates already
# sent to the apiserver, after stopping claim_batch's background creation. release() waits so its
# deletecollection call can cover them, while detach() waits so no creates happen after it gives up the Lease.
BATCH_STOP_CREATION_TIMEOUT_SECONDS = 30.0

_BATCH_ID_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
BATCH_ID_MAX_LENGTH = 52


def validate_batch_id(batch_id: str) -> None:
    """Validates a caller-supplied batch id.

    Must be a DNS-1123 label that starts with a letter and is at most 52 characters,
    so ``<id>-<ordinal>`` stays within a 63-character DNS label limit.
    """
    if (
        not isinstance(batch_id, str)
        or len(batch_id) > BATCH_ID_MAX_LENGTH
        or not _BATCH_ID_RE.match(batch_id)
    ):
        raise ValueError(
            f"batch_id must be a DNS-1123 label starting with a letter, "
            f"at most {BATCH_ID_MAX_LENGTH} characters: {batch_id!r}"
        )


def generate_holder_identity() -> str:
    """Generates a unique Lease holder identity for the current Batch handle user."""
    return f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def _require_duration_exceeds_skew_margin(value: int) -> int:
    if value <= CLOCK_SKEW_MARGIN:
        raise ValueError(f"Duration must be greater than clock skew margin ({CLOCK_SKEW_MARGIN}s)")
    return value


def validate_lease_duration_value(value: object) -> int:
    """Validates that caller-supplied ``leaseDurationSeconds`` value is an int
    and is greater than ``CLOCK_SKEW_MARGIN``.
    """
    if type(value) is not int:
        raise ValueError(f"Duration must be an int, got {type(value).__name__}")
    return _require_duration_exceeds_skew_margin(value)


def parse_lease_duration_annotation(value: str | None) -> int:
    """Parses ``BATCH_LEASE_DURATION_ANNOTATION`` upon ``get_batch`` to
    validate the Lease annotation is valid. If missing, falls back to the default.
    """
    if value is None:
        return BATCH_DEFAULT_LEASE_DURATION_SECONDS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise BatchError(
            f"batch lease duration annotation {value!r} is not a valid integer"
        ) from None
    try:
        return _require_duration_exceeds_skew_margin(parsed)
    except ValueError as e:
        raise BatchError(f"batch lease duration annotation: {e}") from None


def is_lease_stale(
    renew_time: datetime,
    lease_duration_seconds: int,
    now: datetime,
    skew_margin: int = CLOCK_SKEW_MARGIN,
) -> bool:
    """Computes Lease staleness to prevent ``get_batch`` operating on an inactive batch.
    Staleness is computed via ``now > renew_time + lease_duration_seconds - skew_margin``.
    """
    return now > renew_time + timedelta(seconds=lease_duration_seconds - skew_margin)


def parse_ordinal(batch_id: str, claim_name: str) -> int | None:
    """Extracts the ordinal from a ``<batch-id>-<ordinal>`` claim name."""
    prefix = f"{batch_id}-"
    if not claim_name.startswith(prefix):
        return None
    suffix = claim_name[len(prefix):]
    if not suffix.isdigit():
        return None
    return int(suffix)


def compute_next_ordinal(batch_id: str, claim_objs: Sequence[dict]) -> int:
    """Computes the next ordinal for newly attached handles, which do not have it in memory yet."""
    max_ordinal = -1
    for claim_obj in claim_objs:
        name = (claim_obj.get("metadata") or {}).get("name", "")
        ordinal = parse_ordinal(batch_id, name)
        if ordinal is not None and ordinal > max_ordinal:
            max_ordinal = ordinal
    return max_ordinal + 1


def reconstruct_groups(claim_objs: Sequence[dict]) -> list[BatchGroup]:
    """Rebuilds each pool's ``BatchGroup`` from its claims' group annotations.

    A pool with no group annotations on any of its claims becomes ``BatchGroup(size=0, min_ready=0)``,
    and claims that are detected with different annotation values in the same pool raise ``BatchError``.
    """
    by_pool: dict[str, tuple[str, str]] = {}
    pools_seen: set[str] = set()
    for claim_obj in claim_objs:
        spec = claim_obj.get("spec") or {}
        warmpool = (spec.get("warmPoolRef") or {}).get("name", "")
        pools_seen.add(warmpool)
        annotations = (claim_obj.get("metadata") or {}).get("annotations") or {}
        size_str = annotations.get(BATCH_GROUP_SIZE_ANNOTATION)
        min_ready_str = annotations.get(BATCH_GROUP_MIN_READY_ANNOTATION)
        if size_str is None and min_ready_str is None:
            continue
        if size_str is None or min_ready_str is None:
            raise BatchError(
                f"claim {(claim_obj.get('metadata') or {}).get('name')!r} for pool "
                f"{warmpool!r} carries only one of the batch group annotations"
            )
        pair = (size_str, min_ready_str)
        if warmpool in by_pool and by_pool[warmpool] != pair:
            raise BatchError(
                f"pool {warmpool!r} has conflicting batch group annotations "
                "across its claims"
            )
        by_pool[warmpool] = pair

    groups = []
    for pool in sorted(pools_seen):
        if pool in by_pool:
            size_str, min_ready_str = by_pool[pool]
            groups.append(
                BatchGroup(warmpool=pool, size=int(size_str), min_ready=int(min_ready_str))
            )
        else:
            groups.append(BatchGroup(warmpool=pool, size=0, min_ready=0))
    return groups


def derive_member(claim_obj: dict) -> Member:
    """Builds a ``Member`` from a raw SandboxClaim object."""
    metadata = claim_obj.get("metadata") or {}
    spec = claim_obj.get("spec") or {}
    status = claim_obj.get("status") or {}
    conditions = status.get("conditions") or []

    ready = False
    terminal = False
    reason: str | None = None
    message: str | None = None
    ready_condition = next((c for c in conditions if c.get("type") == "Ready"), None)
    if ready_condition is not None:
        reason = ready_condition.get("reason")
        message = ready_condition.get("message")
        if ready_condition.get("status") == "True":
            ready = True
        # We do not treat WarmPoolNotFound or TemplateNotFound as terminal, because the controller
        # requeues those every minute rather than failing the claim.
        elif ready_condition.get("status") == "False" and reason in TERMINAL_CLAIM_READY_REASONS:
            terminal = True

    sandbox_status = status.get("sandbox") or {}
    sandbox_name = sandbox_status.get("name") or None
    ready = ready and sandbox_name is not None

    return Member(
        claim_name=metadata.get("name", ""),
        sandbox_name=sandbox_name,
        warmpool=(spec.get("warmPoolRef") or {}).get("name", ""),
        pod_ips=tuple(sandbox_status.get("podIPs") or []),
        service_fqdn=sandbox_status.get("serviceFQDN"),
        ready=ready,
        terminal=terminal,
        lost=False,
        reason=reason,
        message=message,
    )


class BatchState:
    """The internal state of a batch handle, containing its members and metadata."""

    def __init__(self, batch_id: str, groups: Sequence[BatchGroup]) -> None:
        self.batch_id = batch_id
        self.groups: list[BatchGroup] = list(groups)
        self.size = sum(g.size for g in self.groups)

        self._members: dict[str, Member] = {}
        self._ordinals: dict[str, int] = {}
        self._initial_fill: set[str] = set()
        self._released: set[str] = set()
        # Tracks which members have been returned to the caller for at-most-once per-handle delivery
        self._dispatched: set[str] = set()
        self._next_ordinal = 0
        self._error: Exception | None = None

    def seed_from_claims(self, claim_objs: Sequence[dict]) -> None:
        """Reconstructs state from the batch's existing claims."""
        self._next_ordinal = compute_next_ordinal(self.batch_id, claim_objs)
        for claim_obj in claim_objs:
            self.upsert_claim(claim_obj)

    def upsert_claim(self, claim_obj: dict) -> Member | None:
        """Derives a ``Member`` from a claim and inserts or updates it."""
        metadata = claim_obj.get("metadata") or {}
        claim_name = metadata.get("name", "")
        if claim_name in self._released:
            return None
        ordinal = parse_ordinal(self.batch_id, claim_name)
        if ordinal is not None:
            self._ordinals[claim_name] = ordinal
            if ordinal < self.size:
                self._initial_fill.add(claim_name)
        member = derive_member(claim_obj)
        self._members[claim_name] = member
        return member

    def mark_lost(self, claim_name: str) -> Member | None:
        """Handles a lost claim (watch DELETED or missing on re-list)."""
        # If caller already released the claim, we stop tracking it and return early
        if claim_name in self._released:
            self._members.pop(claim_name, None)
            return None
        existing = self._members.get(claim_name)
        if existing is None:
            # Deleted before this handle ever saw an ADDED/MODIFIED event for it.
            return None
        lost_member = existing.model_copy(
            update={"lost": True, "ready": False, "pod_ips": (), "service_fqdn": None}
        )
        self._members[claim_name] = lost_member
        return lost_member

    def resync_from_list(self, claim_objs: Sequence[dict]) -> None:
        """Replaces the cache with a fresh list after a watch 410."""
        seen_names = set()
        for claim_obj in claim_objs:
            name = (claim_obj.get("metadata") or {}).get("name", "")
            seen_names.add(name)
            self.upsert_claim(claim_obj)
        missing = set(self._members.keys()) - seen_names
        for name in missing:
            self.mark_lost(name)

    def mark_released(self, claim_name: str) -> None:
        """Marks a claim released by the caller."""
        self._released.add(claim_name)
        self._members.pop(claim_name, None)
        self._ordinals.pop(claim_name, None)

    def members(self, warmpool: str | None = None) -> list[Member]:
        """Returns a snapshot of the batch's members, optionally filtered to one pool."""
        items = [
            (self._ordinals.get(name, 0), member)
            for name, member in self._members.items()
            if warmpool is None or member.warmpool == warmpool
        ]
        items.sort(key=itemgetter(0))
        return [m for _, m in items]

    def get_member(self, claim_name: str) -> Member | None:
        """Returns a single member."""
        return self._members.get(claim_name)

    def try_dispatch(self, claim_name: str) -> bool:
        """Tries to mark a member has handed to the caller.
        
        Returns ``False`` if the member was already dispatched for at-most-once per-caller delivery.
        """
        if claim_name in self._dispatched:
            return False
        self._dispatched.add(claim_name)
        return True

    def note_error(self, error: Exception) -> None:
        """Records a sticky error for ``err()``."""
        if self._error is None:
            self._error = error

    def error(self) -> Exception | None:
        return self._error
