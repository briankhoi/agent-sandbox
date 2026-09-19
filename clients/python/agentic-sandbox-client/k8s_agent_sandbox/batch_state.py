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
"""I/O-free batch state core shared by the sync and async batch handles.

Holds the member table, turns raw SandboxClaim objects into ``Member``
snapshots, and owns per-group accounting, ordinal allocation, and group
reconstruction from annotations. No threads, no asyncio, no Kubernetes
calls: the sync and async shells (``sandbox_batch.py``, and its async
counterpart) do all I/O and call into this module under their own lock, so
sync/async parity comes from sharing this one implementation.
"""

import os
import re
import socket
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from .constants import (
    BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    CLOCK_SKEW_MARGIN,
    TERMINAL_CLAIM_READY_REASONS,
)
from .exceptions import BatchError
from .models import BatchGroup, Member

_BATCH_ID_RE = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
BATCH_ID_MAX_LENGTH = 52


def validate_batch_id(batch_id: str) -> None:
    """Validates a caller-supplied batch id.

    Must be a DNS-1123 label that starts with a letter and is at most 52
    characters, so ``<id>-<ordinal>`` stays within a 63-character DNS label
    and label-value limit (proposal, Batch Model 1; plan, "Batch id").
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
    """Generates a per-handle Lease holder identity (plan, OPEN-P)."""
    return f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def validate_lease_duration_value(value: object) -> int:
    """Validates a caller-supplied duration destined for ``leaseDurationSeconds``.

    Must be an ``int`` (``type(v) is int`` rejects floats, including whole
    ones, plus ``bool`` and strings) greater than ``CLOCK_SKEW_MARGIN``.
    Raises ``ValueError``; the boundary case uses the exact message the
    plan's tests assert on.
    """
    if type(value) is not int:
        raise ValueError(
            f"Duration must be an int greater than clock skew margin "
            f"({CLOCK_SKEW_MARGIN}s), got {type(value).__name__}"
        )
    if value <= CLOCK_SKEW_MARGIN:
        raise ValueError(
            f"Duration must be greater than clock skew margin ({CLOCK_SKEW_MARGIN}s)"
        )
    return value


def parse_lease_duration_annotation(value: str | None) -> int:
    """Parses ``BATCH_LEASE_DURATION_ANNOTATION`` for a ``get_batch`` takeover.

    Missing falls back to the default duration (batches from before PR 2,
    or PR 1 tests). Present but unparsable, or not greater than
    ``CLOCK_SKEW_MARGIN``, fails fast with ``BatchError`` so a corrupted or
    hand-edited annotation can never trap the handle in an immediately
    stale Lease.
    """
    if value is None:
        return BATCH_DEFAULT_LEASE_DURATION_SECONDS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise BatchError(
            f"batch lease duration annotation {value!r} is not a valid integer"
        ) from None
    if parsed <= CLOCK_SKEW_MARGIN:
        raise BatchError(
            f"batch lease duration annotation {parsed} must be greater than "
            f"clock skew margin ({CLOCK_SKEW_MARGIN}s)"
        )
    return parsed


def is_lease_stale(
    renew_time: datetime,
    lease_duration_seconds: int,
    now: datetime,
    skew_margin: int = CLOCK_SKEW_MARGIN,
) -> bool:
    """``get_batch``'s staleness test: ``now > renewTime + leaseDurationSeconds - skew_margin``.

    More cautious than the reaper's own ``now > renewTime + leaseDurationSeconds``
    test, so clock drift or network round-trip time can't let ``get_batch``
    adopt a Lease the cluster may already treat as expired.
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
    """The next ordinal to allocate: highest seen ordinal + 1.

    Counts claims with a ``deletionTimestamp`` too, since a released claim
    may still be terminating when its successor would be created (proposal,
    Membership).
    """
    max_ordinal = -1
    for claim_obj in claim_objs:
        name = (claim_obj.get("metadata") or {}).get("name", "")
        ordinal = parse_ordinal(batch_id, name)
        if ordinal is not None and ordinal > max_ordinal:
            max_ordinal = ordinal
    return max_ordinal + 1


def reconstruct_groups(claim_objs: Sequence[dict]) -> list[BatchGroup]:
    """Rebuilds each pool's ``BatchGroup`` from its claims' group annotations.

    A pool with no group annotations on any of its claims becomes
    ``BatchGroup(size=0, min_ready=0)`` (proposal: claims in size=0 groups
    never carry them). Claims of one pool disagreeing on the annotation
    values raises ``BatchError``.
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
    """Builds a ``Member`` snapshot from a raw SandboxClaim object.

    Mirrors the readiness/terminal-reason logic in
    ``K8sHelper._watch_claim``, generalized from "wait for one outcome" to
    "snapshot the current state" for a batch's many claims.
    """
    metadata = claim_obj.get("metadata") or {}
    spec = claim_obj.get("spec") or {}
    status = claim_obj.get("status") or {}
    conditions = status.get("conditions") or []

    terminal = any(cond.get("reason") == "WarmPoolNotFound" for cond in conditions)

    ready = False
    reason: str | None = None
    message: str | None = None
    ready_condition = next((c for c in conditions if c.get("type") == "Ready"), None)
    if ready_condition is not None:
        reason = ready_condition.get("reason")
        message = ready_condition.get("message")
        if ready_condition.get("status") == "True":
            ready = True
        elif ready_condition.get("status") == "False" and (
            reason == "TemplateNotFound" or reason in TERMINAL_CLAIM_READY_REASONS
        ):
            terminal = True

    sandbox_status = status.get("sandbox") or {}
    # Support both 'name' (standard) and 'Name' (legacy, before CRD rename in #440).
    sandbox_name = sandbox_status.get("name") or sandbox_status.get("Name") or None
    ready = ready and sandbox_name is not None

    return Member(
        claim_name=metadata.get("name", ""),
        sandbox_name=sandbox_name,
        warmpool=(spec.get("warmPoolRef") or {}).get("name", ""),
        pod_ips=list(sandbox_status.get("podIPs") or []),
        service_fqdn=sandbox_status.get("serviceFQDN"),
        ready=ready,
        terminal=terminal,
        lost=False,
        reason=reason,
        message=message,
    )


class BatchState:
    """The I/O-free member table and accounting for one batch handle."""

    def __init__(self, batch_id: str, groups: Sequence[BatchGroup]) -> None:
        self.batch_id = batch_id
        self.groups: list[BatchGroup] = list(groups)
        self.size = sum(g.size for g in self.groups)  # Sum of the group sizes.

        self._members: dict[str, Member] = {}
        self._ordinals: dict[str, int] = {}
        self._initial_fill: set[str] = set()
        self._released: set[str] = set()
        self._dispatched: set[str] = set()
        self._next_ordinal = 0
        self._error: Exception | None = None

    def seed_from_claims(self, claim_objs: Sequence[dict]) -> None:
        """Initializes ordinal allocation and the member table from an
        existing batch's claims, for a ``get_batch`` reattachment."""
        self._next_ordinal = compute_next_ordinal(self.batch_id, claim_objs)
        for claim_obj in claim_objs:
            self.apply_claim(claim_obj)

    def apply_claim(self, claim_obj: dict) -> Member | None:
        """Derives a ``Member`` from an ADDED/MODIFIED claim and stores it.

        Returns ``None``, leaving the member table untouched, if the caller
        already released this claim (OPEN-5): a stray late event must not
        resurrect it.
        """
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

    def mark_deleted(self, claim_name: str) -> Member | None:
        """Handles a claim's disappearance (watch DELETED, or missing on re-list).

        Returns the resulting lost ``Member``, or ``None`` if the caller
        had already released it, in which case it is simply dropped
        instead of being reported as lost.
        """
        if claim_name in self._released:
            self._members.pop(claim_name, None)
            return None
        existing = self._members.get(claim_name)
        if existing is None:
            return None
        lost_member = existing.model_copy(update={"lost": True})
        self._members[claim_name] = lost_member
        return lost_member

    def reconcile_list(self, claim_objs: Sequence[dict]) -> None:
        """Replaces the cache with a fresh list after a watch 410: any
        previously-known, now-missing claim becomes lost."""
        seen_names = set()
        for claim_obj in claim_objs:
            name = (claim_obj.get("metadata") or {}).get("name", "")
            seen_names.add(name)
            self.apply_claim(claim_obj)
        missing = set(self._members.keys()) - seen_names
        for name in missing:
            self.mark_deleted(name)

    def mark_released(self, claim_name: str) -> None:
        """Marks a member as released by the caller, so it is dropped from
        ``members()`` and never reported as lost (OPEN-5)."""
        self._released.add(claim_name)
        self._members.pop(claim_name, None)

    def members(self, warmpool: str | None = None) -> list[Member]:
        """A snapshot sorted by ordinal, optionally filtered to one pool."""
        items = [
            (self._ordinals.get(name, 0), member)
            for name, member in self._members.items()
        ]
        if warmpool is not None:
            items = [(ordinal, m) for ordinal, m in items if m.warmpool == warmpool]
        items.sort(key=lambda pair: pair[0])
        return [m for _, m in items]

    def get(self, claim_name: str) -> Member | None:
        return self._members.get(claim_name)

    def try_dispatch(self, claim_name: str) -> bool:
        """Marks a member as handed out to the caller.

        Returns ``False`` if it was already dispatched, so every hand-out
        path (``events()``, ``iter_ready_groups()``, ``wait_for_quorum()``,
        ``acquire()``/``replace()``) can share this one at-most-once check.
        """
        if claim_name in self._dispatched:
            return False
        self._dispatched.add(claim_name)
        return True

    def note_error(self, error: Exception) -> None:
        """Records a sticky error for ``err()``; the first one wins."""
        if self._error is None:
            self._error = error

    def error(self) -> Exception | None:
        return self._error
