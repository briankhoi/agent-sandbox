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

"""Batch id, argument, and Lease annotation validation helpers shared by both sync and async batch handles."""

import dataclasses
import os
import re
import secrets
import socket
import string
import uuid
from collections.abc import Mapping, Sequence

from .batch_state import (
    BATCH_DEFAULT_CREATE_RPS,
    BATCH_DEFAULT_LEASE_DURATION_SECONDS,
    BATCH_DEFAULT_MAX_IN_FLIGHT,
    BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS,
    BATCH_DEFAULT_WORK_BUDGET_SECONDS,
    CLOCK_SKEW_MARGIN,
)
from .constants import BATCH_ID_LABEL
from .exceptions import BatchError
from .models import BatchGroup
from .pod_metadata import validate_labels

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


_BATCH_ID_ALPHABET = string.ascii_lowercase + string.digits
_GENERATED_BATCH_ID_RANDOM_LENGTH = 11


def generate_batch_id() -> str:
    """Generates a batch id: ``"b"`` followed by 11 random ``[a-z0-9]`` characters."""
    return "b" + "".join(
        secrets.choice(_BATCH_ID_ALPHABET) for _ in range(_GENERATED_BATCH_ID_RANDOM_LENGTH)
    )


def generate_holder_identity() -> str:
    """Generates a unique Lease holder identity for the current Batch handle user."""
    return f"{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an int, got {type(value).__name__}")
    return value


def _parse_int_annotation(value: str, name: str) -> int:
    """Parses an integer Lease annotation, raising ``BatchError`` upon error."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BatchError(f"batch {name} annotation {value!r} is not a valid integer") from None


def _require_duration_exceeds_skew_margin(value: int) -> int:
    if value <= CLOCK_SKEW_MARGIN:
        raise ValueError(f"Duration must be greater than clock skew margin ({CLOCK_SKEW_MARGIN}s)")
    return value


def validate_lease_duration_value(value: object) -> int:
    """Validates that caller-supplied ``leaseDurationSeconds`` value is an int and is greater than ``CLOCK_SKEW_MARGIN``."""
    return _require_duration_exceeds_skew_margin(_require_int(value, "Duration"))


def parse_lease_duration_annotation(value: str | None) -> int:
    """Parses ``BATCH_LEASE_DURATION_ANNOTATION`` upon ``get_batch`` to
    validate the Lease annotation is valid. If missing, falls back to the default.
    """
    if value is None:
        return BATCH_DEFAULT_LEASE_DURATION_SECONDS
    parsed = _parse_int_annotation(value, "lease duration")
    try:
        return _require_duration_exceeds_skew_margin(parsed)
    except ValueError as e:
        raise BatchError(f"batch lease duration annotation: {e}") from None


def validate_positive_int(value: object, name: str) -> int:
    value = _require_int(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be greater than 0, got {value}")
    return value


def parse_positive_int_annotation(value: str | None, default: int, name: str) -> int:
    """Parses a positive-integer Lease annotation.

    A missing annotation falls back to ``default``, and a non-positive integer raises ``BatchError``.
    """
    if value is None:
        return default
    parsed = _parse_int_annotation(value, name)
    if parsed <= 0:
        raise BatchError(f"batch {name} annotation {value!r} must be a positive integer")
    return parsed


@dataclasses.dataclass(frozen=True)
class ClaimBatchArgs:
    """The validated arguments of a ``claim_batch`` call."""
    groups: list[BatchGroup]
    labels: dict[str, str]
    batch_id: str
    create_rps: float
    max_in_flight: int
    work_budget: int
    quorum_timeout: int
    lease_duration: int


def validate_claim_batch_args(
    groups: Sequence[BatchGroup],
    labels: Mapping[str, str] | None,
    batch_id: str | None,
    create_rps: float | None,
    max_in_flight: int | None,
    work_budget: int | None,
    quorum_timeout: int | None,
    lease_duration: int | None,
) -> ClaimBatchArgs:
    """Validates ``claim_batch``'s arguments and fills in defaults for empty values."""
    groups = list(groups)
    if not groups:
        raise ValueError("groups must contain at least one BatchGroup")
    seen_pools: set[str] = set()
    for group in groups:
        if not isinstance(group, BatchGroup):
            raise ValueError(f"groups must be BatchGroup instances, got {type(group).__name__}")
        # BatchGroup itself allows size=0 (a lazy group) and already validates min_ready against size.
        if group.size <= 0:
            raise ValueError(
                f"group {group.warmpool!r} has size {group.size}; claim_batch requires size > 0"
            )
        if group.warmpool in seen_pools:
            raise ValueError(f"duplicate warmpool {group.warmpool!r} in groups")
        seen_pools.add(group.warmpool)

    resolved_labels = dict(labels or {})
    if BATCH_ID_LABEL in resolved_labels:
        raise ValueError(f"labels must not set the reserved {BATCH_ID_LABEL!r} label")
    validate_labels(resolved_labels)

    if batch_id is None:
        batch_id = generate_batch_id()
    else:
        validate_batch_id(batch_id)

    if create_rps is None:
        create_rps = BATCH_DEFAULT_CREATE_RPS
    elif type(create_rps) not in (int, float) or not create_rps > 0:
        raise ValueError(f"create_rps must be a positive number, got {create_rps!r}")

    max_in_flight = (
        BATCH_DEFAULT_MAX_IN_FLIGHT
        if max_in_flight is None
        else validate_positive_int(max_in_flight, "max_in_flight")
    )
    work_budget = (
        BATCH_DEFAULT_WORK_BUDGET_SECONDS
        if work_budget is None
        else validate_positive_int(work_budget, "work_budget")
    )
    quorum_timeout = (
        BATCH_DEFAULT_QUORUM_TIMEOUT_SECONDS
        if quorum_timeout is None
        else validate_positive_int(quorum_timeout, "quorum_timeout")
    )
    lease_duration = (
        BATCH_DEFAULT_LEASE_DURATION_SECONDS
        if lease_duration is None
        else validate_lease_duration_value(lease_duration)
    )

    return ClaimBatchArgs(
        groups=groups,
        labels=resolved_labels,
        batch_id=batch_id,
        create_rps=float(create_rps),
        max_in_flight=max_in_flight,
        work_budget=work_budget,
        quorum_timeout=quorum_timeout,
        lease_duration=lease_duration,
    )
