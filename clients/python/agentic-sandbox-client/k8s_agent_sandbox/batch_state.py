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

``BatchState`` flows one way: inputs (watch updates, create results, releases) update the member
table and call ``_reclassify``, which files each initial-fill member into its group's sets;
``_evaluate_group`` then decides the group's quorum verdict; and the two consumers read the
results, ``collect_events()`` for ``events()`` and ``pop_group_verdicts()`` for ``iter_ready_groups()``.
"""

import dataclasses
import random
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from enum import Enum
from operator import itemgetter

from .constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    TERMINAL_CLAIM_READY_REASONS,
)
from .exceptions import BatchError, QuorumUnreachableError
from .models import BatchEvent, BatchEventType, BatchGroup, GroupReady, Member

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

class CreateOutcome(str, Enum):
    """How the create-retry helper should treat one attempt's error."""
    SUCCESS = "success"
    RETRY = "retry"
    FAIL = "fail"


def classify_create_error(status: int | None, attempt: int) -> CreateOutcome:
    """Classifies a failed claim create attempt (1-based ``attempt``).

    - 409 on the first attempt is a name collision and fails. On a later attempt it means an
      earlier attempt landed, since claim names are deterministic, so it counts as success.
    - 429, 5xx, and transport errors (``status is None``) retry, up to ``BATCH_CREATE_MAX_ATTEMPTS``.
    - Everything else (400/403/404/422, ...) fails without retrying.
    """
    if status == 409:
        return CreateOutcome.SUCCESS if attempt > 1 else CreateOutcome.FAIL
    retryable = status is None or status == 429 or 500 <= status < 600
    if retryable and attempt < BATCH_CREATE_MAX_ATTEMPTS:
        return CreateOutcome.RETRY
    return CreateOutcome.FAIL


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Returns a ``Retry-After`` header's delay in seconds, or ``None``.

    Only the delta-seconds form is honored; the HTTP-date form is ignored. Both clients'
    ``ApiException.headers`` (urllib3's ``HTTPHeaderDict``, aiohttp's ``CIMultiDictProxy``)
    are case-insensitive, so a plain lookup is enough.
    """
    value = (headers or {}).get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, BATCH_CREATE_RETRY_AFTER_MAX_SECONDS)


def create_backoff_delay(
    attempt: int, retry_after: float | None, rand: Callable[[], float] = random.random
) -> float:
    """Returns how long to wait after failed attempt ``attempt`` (1-based) before the next one.

    Honors ``Retry-After`` when present; otherwise exponential backoff with equal jitter,
    so concurrent retries against a throttled apiserver spread out instead of colliding.
    """
    if retry_after is not None:
        return retry_after
    ceiling = min(
        BATCH_CREATE_BACKOFF_MAX_SECONDS,
        BATCH_CREATE_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)),
    )
    return ceiling / 2 + rand() * ceiling / 2


class CreatePacer:
    """A token bucket with capacity 1 that spaces create starts ``1 / rate`` seconds apart.

    A capacity above 1 would let a burst at the start of a window combine with refills
    inside it, so more than ``rate`` creates could start within one second.
    """

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._anchor: float | None = None
        self._count = 0

    def reserve(self, now: float) -> float:
        """Reserves the next start slot and returns its time (never earlier than ``now``)."""
        # Slots are anchor + count / rate rather than a running sum of 1 / rate, so float
        # rounding can't pull a slot early and squeeze an extra start into a second.
        if self._anchor is None or now > self._anchor + self._count / self._rate:
            self._anchor = now
            self._count = 0
        start = self._anchor + self._count / self._rate
        self._count += 1
        return start


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


def reconstruct_groups(claim_objs: Sequence[dict], batch_id: str | None = None) -> list[BatchGroup]:
    """Rebuilds each pool's ``BatchGroup`` from its claims' group annotations.

    A pool with no group annotations on any of its claims becomes ``BatchGroup(size=0, min_ready=0)``,
    and claims that are detected with different annotation values in the same pool raise ``BatchError``.

    Groups are ordered by each pool's lowest ordinal, which is the order ``claim_batch`` was given
    them in. Pools with no claim whose ordinal parses (or with no ``batch_id``) sort last, by name.
    """
    by_pool: dict[str, tuple[str, str]] = {}
    pools_seen: set[str] = set()
    lowest_ordinal: dict[str, int] = {}
    for claim_obj in claim_objs:
        spec = claim_obj.get("spec") or {}
        warmpool = (spec.get("warmPoolRef") or {}).get("name", "")
        pools_seen.add(warmpool)
        if batch_id is not None:
            ordinal = parse_ordinal(batch_id, (claim_obj.get("metadata") or {}).get("name", ""))
            if ordinal is not None and ordinal < lowest_ordinal.get(warmpool, ordinal + 1):
                lowest_ordinal[warmpool] = ordinal
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

    def order_key(pool: str) -> tuple[int, int, str]:
        if pool in lowest_ordinal:
            return (0, lowest_ordinal[pool], pool)
        return (1, 0, pool)

    groups = []
    for pool in sorted(pools_seen, key=order_key):
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


CREATE_FAILED_REASON = "CreateFailed"


class ConsumerMode(str, Enum):
    """How a handle's Ready members are handed out, fixed by the first consumer call (OPEN-F)."""
    STREAM = "stream"  # events() was called first; no per-group quorum.
    QUORUM = "quorum"  # iter_ready_groups() was called first; events() holds back each group's quorum.


# Reasons an initial-fill member can no longer arrive, as reported in QuorumUnreachableError.
_REASON_TERMINAL = "terminal"
_REASON_LOST = "lost"
_REASON_CREATE_FAILED = "create_failed"
_REASON_RELEASED = "released"


@dataclasses.dataclass
class _GroupFill:
    """Tracks one group's progress through the initial fill. Its sets are updated as each member
    changes, so checking whether the group has reached, or can still reach, its quorum never needs
    to scan every member.
    """
    group: BatchGroup
    # Ready initial-fill members not yet handed to a consumer.
    ready_undispatched: set[str] = dataclasses.field(default_factory=set)
    # Members that can no longer arrive, by reason.
    cannot_arrive: dict[str, str] = dataclasses.field(default_factory=dict)
    # Initial-fill claims already gone when a handle attached.
    missing: int = 0
    # The group's one iter_ready_groups() outcome, once it has one.
    verdict: GroupReady | None = None

    def cannot_arrive_counts(self) -> Counter[str]:
        counts = Counter(self.cannot_arrive.values())
        counts[_REASON_LOST] += self.missing
        return counts

    def reachable(self) -> int:
        return self.group.size - len(self.cannot_arrive) - self.missing


class _LeaseDegraded:
    """Marks where a ``LEASE_DEGRADED`` event falls among pending member changes."""


class BatchState:
    """The internal state of a batch handle, containing its members and metadata.

    Used for tracking the initial fill (we define as the initial set of claims created
    by ``claim_batch``, quorum group tracking, settle detection, and delivering
    members to the caller via ConsumerMode.
    """

    def __init__(self, batch_id: str, groups: Sequence[BatchGroup]) -> None:
        self.batch_id = batch_id
        self.groups: list[BatchGroup] = list(groups)
        self.size = sum(g.size for g in self.groups)

        self._members: dict[str, Member] = {} # all claims the handle is aware of
        self._ordinals: dict[str, int] = {}   # claim name to ordinal (the N in "<batch_id>-N"), used to sort members
        self._initial_fill: set[str] = set()  # name of claims initially created by claim_batch
        self._released: set[str] = set()      # claims the caller has released
        self._dispatched: set[str] = set()    # members handed out as usable, at most once per handle
        self._next_ordinal = 0
        self._error: Exception | None = None

        self._group_fills: dict[str, _GroupFill] = {g.warmpool: _GroupFill(g) for g in self.groups} # warmpool to its initial fill members
        self._warmpool_of: dict[str, str] = {} # claim name to warmpool name
        self._create_failed: set[str] = set() # claims whose create call failed permanently
        self._unsettled: set[str] = set()     # initial fill members that can still arrive

        self._mode: ConsumerMode | None = None      # mode to return members back to caller
        self._events_claimed = False                # bool to enforce that only one consumer of events() is allowed
        self._undelivered_verdicts: list[GroupReady] = [] # finished group results waiting to be emitted
        # initial fill-claims whose event state may have changed, and queued LEASE_DEGRADED events,
        # in change order since last collect_events()
        self._pending_changes: dict[str | _LeaseDegraded, None] = {}
        self._reported: set[str] = set()      # members whose failure or loss events() already announced

        self._fill_deadline: float | None = None # monotonic time at which the initial fill gives up (quorum_timeout)
        self._deadline_passed = False
        self._closed = False

    def seed_from_claims(self, claim_objs: Sequence[dict]) -> None:
        """Reconstructs state from the batch's existing claims."""
        self._next_ordinal = max(self._next_ordinal, compute_next_ordinal(self.batch_id, claim_objs))
        # In ordinal order, so members already Ready at attach reach events() in ordinal order (OPEN-G).
        for claim_obj in sorted(claim_objs, key=self._ordinal_sort_key):
            self.upsert_claim(claim_obj)
        # An initial-fill claim absent at attach time was deleted before this handle saw it,
        # so it can no longer arrive.
        known = Counter(self._warmpool_of[name] for name in self._initial_fill if name in self._warmpool_of)
        for fill in self._group_fills.values():
            fill.missing = max(0, fill.group.size - known[fill.group.warmpool])
            self._evaluate_group(fill)

    def _ordinal_sort_key(self, claim_obj: dict) -> tuple[bool, int]:
        ordinal = parse_ordinal(self.batch_id, (claim_obj.get("metadata") or {}).get("name", ""))
        return (ordinal is None, ordinal or 0)

    def plan_initial_fill(self) -> list[tuple[str, BatchGroup]]:
        """Assigns ordinals ``0..size-1`` across the groups in order and returns ``(claim_name, group)``
        per planned claim.

        Every name is known before the first create, so creation workers never allocate an ordinal.
        """
        plan: list[tuple[str, BatchGroup]] = []
        ordinal = 0
        for group in self.groups:
            for _ in range(group.size):
                name = f"{self.batch_id}-{ordinal}"
                self._ordinals[name] = ordinal
                self._initial_fill.add(name)
                self._warmpool_of[name] = group.warmpool
                self._reclassify(name)
                plan.append((name, group))
                ordinal += 1
        self._next_ordinal = max(self._next_ordinal, ordinal)
        return plan

    def upsert_claim(self, claim_obj: dict) -> Member | None:
        """Derives a ``Member`` from a claim and inserts or updates it."""
        metadata = claim_obj.get("metadata") or {}
        claim_name = metadata.get("name", "")
        if claim_name in self._released:
            return None
        # A create-failed member stays failed: its group has already counted it, and possibly
        # cancelled creates because of it. release()'s deletecollection removes the claim if it
        # landed anyway.
        if claim_name in self._create_failed:
            return self._members.get(claim_name)
        ordinal = parse_ordinal(self.batch_id, claim_name)
        member = derive_member(claim_obj)
        if ordinal is not None:
            self._ordinals[claim_name] = ordinal
            if ordinal < self.size:
                self._initial_fill.add(claim_name)
                self._warmpool_of.setdefault(claim_name, member.warmpool)
        self._members[claim_name] = member
        self._reclassify(claim_name)
        return member

    def mark_lost(self, claim_name: str) -> Member | None:
        """Handles a lost claim (watch DELETED or missing on re-list)."""
        # If caller already released the claim, we stop tracking it and return early
        if claim_name in self._released:
            self._members.pop(claim_name, None)
            return None
        if claim_name in self._create_failed:
            return None
        existing = self._members.get(claim_name)
        if existing is None:
            # Deleted before this handle ever saw an ADDED/MODIFIED event for it.
            return None
        lost_member = existing.model_copy(
            update={"lost": True, "ready": False, "pod_ips": (), "service_fqdn": None}
        )
        self._members[claim_name] = lost_member
        self._reclassify(claim_name)
        return lost_member

    def resync_from_list(self, claim_objs: Sequence[dict]) -> None:
        """Replaces the cache with a fresh list after a watch 410."""
        seen_names = set()
        for claim_obj in claim_objs:
            name = (claim_obj.get("metadata") or {}).get("name", "")
            seen_names.add(name)
            self.upsert_claim(claim_obj)
        missing = set(self._members.keys()) - seen_names - self._create_failed
        for name in missing:
            self.mark_lost(name)

    def mark_released(self, claim_name: str) -> None:
        """Marks a claim released by the caller."""
        self._released.add(claim_name)
        self._members.pop(claim_name, None)
        self._ordinals.pop(claim_name, None)
        self._reclassify(claim_name)

    def mark_create_failed(self, claim_name: str, message: str) -> bool:
        """Records a planned claim whose create ultimately failed as a synthetic terminal member.

        Returns ``True`` once the member's group is past its fail-fast threshold
        (``create_failed > size - min_ready``, OPEN-S) in quorum mode, telling the caller to cancel
        that group's not-yet-started creates. Outside quorum mode ``min_ready`` means nothing to
        the consumer, so this always returns ``False``; ``claim_groups_consumer`` catches up on
        groups that crossed the threshold before quorum mode was fixed. The batch itself is never
        released here.
        """
        pool = self._warmpool_of.get(claim_name, "")
        self._create_failed.add(claim_name)
        self._members[claim_name] = Member(
            claim_name=claim_name,
            warmpool=pool,
            terminal=True,
            reason=CREATE_FAILED_REASON,
            message=message,
        )
        self._reclassify(claim_name)
        return self._mode is ConsumerMode.QUORUM and self._past_create_failure_threshold(pool)

    def mark_create_cancelled(self, claim_name: str) -> None:
        """Drops a planned claim that fail-fast cancelled before it was created from the fill.

        Its group already has its unreachable verdict, so only settle detection needs to forget it.
        """
        self._initial_fill.discard(claim_name)
        self._unsettled.discard(claim_name)

    def note_lease_degraded(self) -> None:
        """Queues a ``LEASE_DEGRADED`` event; called once per degraded episode."""
        self._pending_changes[_LeaseDegraded()] = None

    def note_error(self, error: Exception) -> None:
        """Records a sticky error for ``err()``."""
        if self._error is None:
            self._error = error

    # --- Initial-fill accounting ---

    def _reclassify(self, claim_name: str) -> None:
        """Re-derives one initial-fill member's accounting after any change to it."""
        if claim_name not in self._initial_fill:
            return
        member = self._members.get(claim_name)

        ready = False
        reason: str | None = None
        if claim_name in self._released:
            reason = _REASON_RELEASED
        elif claim_name in self._create_failed:
            reason = _REASON_CREATE_FAILED
        elif member is not None:
            if member.lost:
                reason = _REASON_LOST
            elif member.terminal:
                reason = _REASON_TERMINAL
            elif member.ready:
                ready = True

        fill = self._group_fills.get(self._warmpool_of.get(claim_name, ""))
        if fill is not None:
            if ready and claim_name not in self._dispatched:
                fill.ready_undispatched.add(claim_name)
            else:
                fill.ready_undispatched.discard(claim_name)
            if reason is not None:
                fill.cannot_arrive[claim_name] = reason
            else:
                fill.cannot_arrive.pop(claim_name, None)

        if ready or reason is not None:
            self._unsettled.discard(claim_name)
        else:
            self._unsettled.add(claim_name)

        self._mark_changed(claim_name)
        if fill is not None:
            self._evaluate_group(fill)

    def _mark_changed(self, claim_name: str) -> None:
        # Re-inserting moves the claim to the end, so collect_events() sees its latest change in order.
        self._pending_changes.pop(claim_name, None)
        self._pending_changes[claim_name] = None

    def _evaluate_group(self, fill: _GroupFill) -> None:
        """Gives a group its one ``iter_ready_groups()`` verdict once it has one (quorum mode only)."""
        group = fill.group
        if (
            self._mode is not ConsumerMode.QUORUM
            or self._closed
            or fill.verdict is not None
            or group.size == 0
            or group.min_ready is None
        ):
            return
        if len(fill.ready_undispatched) >= group.min_ready:
            chosen = sorted(fill.ready_undispatched, key=lambda name: self._ordinals[name])[: group.min_ready]
            members = [self._members[name] for name in chosen if self.try_dispatch(name)]
            self._set_verdict(fill, GroupReady(warmpool=group.warmpool, members=members))
        elif fill.reachable() < group.min_ready:
            self._set_verdict(
                fill, GroupReady(warmpool=group.warmpool, error=self._unreachable_error(fill))
            )
        elif self._deadline_passed:
            self._set_verdict(
                fill,
                GroupReady(warmpool=group.warmpool, error=TimeoutError("Group quorum timed out")),
            )

    def _set_verdict(self, fill: _GroupFill, verdict: GroupReady) -> None:
        fill.verdict = verdict
        self._undelivered_verdicts.append(verdict)
        if verdict.error is not None:
            # The group's held-back members were waiting for a quorum consumer that will now
            # never take them, so release them to events().
            for name in sorted(fill.ready_undispatched, key=lambda name: self._ordinals[name]):
                self._mark_changed(name)

    def _unreachable_error(self, fill: _GroupFill) -> QuorumUnreachableError:
        group = fill.group
        counts = fill.cannot_arrive_counts()
        return QuorumUnreachableError(
            f"group {group.warmpool!r} cannot reach min_ready={group.min_ready} of size={group.size}: "
            f"terminal={counts[_REASON_TERMINAL]}, lost={counts[_REASON_LOST]}, "
            f"create_failed={counts[_REASON_CREATE_FAILED]}, released={counts[_REASON_RELEASED]}",
            warmpool=group.warmpool,
            size=group.size,
            min_ready=group.min_ready if group.min_ready is not None else group.size,
            terminal=counts[_REASON_TERMINAL],
            lost=counts[_REASON_LOST],
            create_failed=counts[_REASON_CREATE_FAILED],
            released=counts[_REASON_RELEASED],
        )

    def _past_create_failure_threshold(self, pool: str) -> bool:
        fill = self._group_fills.get(pool)
        if fill is None or fill.group.size == 0 or fill.group.min_ready is None:
            return False
        return fill.cannot_arrive_counts()[_REASON_CREATE_FAILED] > fill.group.size - fill.group.min_ready

    def set_fill_deadline(self, deadline: float) -> None:
        """Sets the monotonic time at which the initial fill settles regardless (``quorum_timeout``)."""
        self._fill_deadline = deadline

    def pending_deadline(self) -> float | None:
        """The fill deadline, or ``None`` once it has passed (or if none is set)."""
        return None if self._deadline_passed else self._fill_deadline

    def check_deadline(self, now: float) -> bool:
        """Applies ``quorum_timeout`` once ``now`` reaches the fill deadline.

        Pending groups get a ``TimeoutError`` verdict (OPEN-J) and pending members stop holding
        ``events()`` open (OPEN-X). Returns ``True`` if this call changed anything.
        """
        if self._deadline_passed or self._fill_deadline is None or now < self._fill_deadline:
            return False
        self._deadline_passed = True
        for fill in self._group_fills.values():
            self._evaluate_group(fill)
        return True

    def is_settled(self) -> bool:
        """Whether no initial-fill member can still arrive: each is Ready, terminal, lost,
        released, or create-failed, or the fill deadline has passed.
        """
        return not self._unsettled or self._deadline_passed

    def _all_groups_have_verdicts(self) -> bool:
        return all(f.verdict is not None for f in self._group_fills.values() if f.group.size > 0)

    # --- Consumer mode ---

    def claim_events_consumer(self) -> None:
        """Registers the single ``events()`` consumer, fixing stream mode if no mode is set yet."""
        self._raise_if_closed()
        if self._events_claimed:
            raise BatchError(f"batch '{self.batch_id}' events() already has a consumer")
        self._events_claimed = True
        if self._mode is None:
            self._mode = ConsumerMode.STREAM

    def claim_groups_consumer(self) -> list[str]:
        """Registers the single ``iter_ready_groups()`` consumer, fixing quorum mode.

        Returns the pools already past their create fail-fast threshold, whose remaining creates
        the caller should now cancel. Raises ``BatchError`` if ``events()`` already fixed stream mode.
        """
        self._raise_if_closed()
        if self._mode is ConsumerMode.STREAM:
            raise BatchError(
                f"batch '{self.batch_id}' is in stream-only mode because events() was called "
                "first; call iter_ready_groups() before calling events()"
            )
        if self._mode is ConsumerMode.QUORUM:
            raise BatchError(f"batch '{self.batch_id}' iter_ready_groups() already has a consumer")
        self._mode = ConsumerMode.QUORUM
        # Quorum is level-triggered: a group that already meets min_ready (e.g. on a
        # re-attached handle) gets its verdict now, without waiting for another event.
        for fill in self._group_fills.values():
            self._evaluate_group(fill)
        return [pool for pool in self._group_fills if self._past_create_failure_threshold(pool)]

    def _is_held_for_quorum(self, claim_name: str) -> bool:
        """In quorum mode, a non-zero group's Ready members wait for its verdict (OPEN-F)."""
        if self._mode is not ConsumerMode.QUORUM:
            return False
        fill = self._group_fills.get(self._warmpool_of.get(claim_name, ""))
        return fill is not None and fill.group.size > 0 and fill.verdict is None

    def collect_events(self) -> tuple[list[BatchEvent], bool]:
        """Returns the ``events()`` output since the last call, and whether the stream is done.

        The stream is done once the initial fill settles and, in quorum mode, every group has its
        verdict (so no held-back member is left to release), or once the batch is closed.
        """
        if self._closed:
            return [], True

        events: list[BatchEvent] = []
        changes, self._pending_changes = self._pending_changes, {}
        for change in changes:
            if isinstance(change, _LeaseDegraded):
                events.append(BatchEvent(type=BatchEventType.LEASE_DEGRADED))
                continue
            member = self._members.get(change)
            if member is None or change in self._reported:
                continue
            if member.ready:
                if self._is_held_for_quorum(change) or not self.try_dispatch(change):
                    continue
                events.append(BatchEvent(type=BatchEventType.MEMBER_READY, member=member))
            elif member.lost or member.terminal:
                self._reported.add(change)
                event_type = BatchEventType.MEMBER_LOST if member.lost else BatchEventType.MEMBER_FAILED
                events.append(BatchEvent(type=event_type, member=member))

        done = self.is_settled() and not self._pending_changes
        if self._mode is ConsumerMode.QUORUM:
            done = done and self._all_groups_have_verdicts()
        return events, done

    def pop_group_verdicts(self) -> tuple[list[GroupReady], bool]:
        """Returns the group verdicts not yet handed to ``iter_ready_groups()``, and whether every
        non-zero group has now been handed its verdict.
        """
        self._raise_if_closed()
        verdicts, self._undelivered_verdicts = self._undelivered_verdicts, []
        return verdicts, self._all_groups_have_verdicts()

    def try_dispatch(self, claim_name: str) -> bool:
        """Tries to mark a member has handed to the caller.
        
        Returns ``False`` if the member was already dispatched for at-most-once per-caller delivery.
        """
        if claim_name in self._dispatched:
            return False
        self._dispatched.add(claim_name)
        fill = self._group_fills.get(self._warmpool_of.get(claim_name, ""))
        if fill is not None:
            fill.ready_undispatched.discard(claim_name)
        return True

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

    def error(self) -> Exception | None:
        return self._error

    def close(self) -> None:
        """Ends ``events()`` and makes quorum waiters raise; called on ``release()``/``detach()``."""
        self._closed = True

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise BatchError(f"batch '{self.batch_id}' has been released or detached")
