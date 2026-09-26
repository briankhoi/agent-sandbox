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

from collections import deque
from collections.abc import Sequence
from operator import itemgetter

from .constants import (
    BATCH_GROUP_MIN_READY_ANNOTATION,
    BATCH_GROUP_SIZE_ANNOTATION,
    TERMINAL_CLAIM_READY_REASONS,
)
from .exceptions import BatchError, QuorumUnreachableError
from .models import BatchEvent, BatchEventType, BatchGroup, GroupReady, Member

CREATE_FAILED_REASON = "CreateFailed"
STREAM_MODE = "stream"
QUORUM_MODE = "quorum"


def parse_ordinal(batch_id: str, claim_name: str) -> int | None:
    """Extracts the ordinal from a ``<batch-id>-<ordinal>`` claim name."""
    prefix = f"{batch_id}-"
    if not claim_name.startswith(prefix):
        return None
    suffix = claim_name[len(prefix):]
    if not suffix.isdigit():
        return None
    return int(suffix)


def reconstruct_groups(claim_objs: Sequence[dict]) -> list[BatchGroup]:
    """Rebuilds each pool's ``BatchGroup`` from its claims' group annotations.

    A pool with no group annotations on any of its claims becomes ``BatchGroup(size=0, min_ready=0)``,
    and claims that are detected with different or invalid annotation values in the same pool raise ``BatchError``.
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
            try:
                groups.append(
                    BatchGroup(warmpool=pool, size=int(size_str), min_ready=int(min_ready_str))
                )
            except ValueError as e:
                # pydantic's ValidationError is a ValueError too, so this covers both
                # non-integer values and a size/min_ready pair BatchGroup rejects.
                raise BatchError(
                    f"pool {pool!r} has invalid batch group annotations "
                    f"(size={size_str!r}, min_ready={min_ready_str!r})"
                ) from e
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
    """The internal state of a batch handle, containing its members and metadata.

    Also does the fill accounting behind ``events()`` and ``iter_ready_groups()``. The fill is
    the batch's initial claims, ordinals ``0..size-1``. Each transition of a fill member is
    turned into queued events and group results as it happens, in O(1), so the handles only feed
    in watch events and pop results.
    """

    def __init__(self, batch_id: str, groups: Sequence[BatchGroup]) -> None:
        self.batch_id = batch_id
        self.groups: list[BatchGroup] = list(groups)
        self.size = sum(g.size for g in self.groups)

        self._members: dict[str, Member] = {}
        self._ordinals: dict[str, int] = {}
        self._error: Exception | None = None

        self._group_by_pool: dict[str, BatchGroup] = {g.warmpool: g for g in self.groups}
        self._mode: str | None = None
        # Per pool, the Ready fill members not yet handed out. A dict is used as an ordered set,
        # so the order is the order members became Ready.
        self._waiting: dict[str, dict[str, None]] = {pool: {} for pool in self._group_by_pool}
        self._dispatched: set[str] = set()
        # Fill members that can no longer become Ready. A member is added once and never removed,
        # so verdicts and the settle point only move forward even if a claim reappears.
        self._unable: set[str] = set()
        self._failed: dict[str, int] = dict.fromkeys(self._group_by_pool, 0)
        self._lost: dict[str, int] = dict.fromkeys(self._group_by_pool, 0)
        self._skipped = 0
        self._missing = 0
        self._ready_count = 0
        self._verdicts: dict[str, GroupReady] = {}
        self._events: deque[BatchEvent] = deque()
        self._group_results: deque[GroupReady] = deque()
        self._fill_expired = False

    def seed_from_claims(self, claim_objs: Sequence[dict]) -> None:
        """Reconstructs state from the batch's existing claims.

        Claims are applied in ordinal order, so members that are already Ready are handed out
        lowest ordinal first.
        """
        def ordinal_key(claim_obj: dict) -> tuple[bool, int]:
            name = (claim_obj.get("metadata") or {}).get("name", "")
            ordinal = parse_ordinal(self.batch_id, name)
            return (ordinal is None, ordinal or 0)

        for claim_obj in sorted(claim_objs, key=ordinal_key):
            self.upsert_claim(claim_obj)

    def upsert_claim(self, claim_obj: dict) -> Member | None:
        """Derives a ``Member`` from a claim and inserts or updates it."""
        metadata = claim_obj.get("metadata") or {}
        claim_name = metadata.get("name", "")
        ordinal = parse_ordinal(self.batch_id, claim_name)
        if ordinal is not None:
            self._ordinals[claim_name] = ordinal
        member = derive_member(claim_obj)
        previous = self._members.get(claim_name)
        self._members[claim_name] = member
        self._on_fill_change(claim_name, member, previous)
        return member

    def mark_lost(self, claim_name: str) -> Member | None:
        """Handles a lost claim (watch DELETED or missing on re-list)."""
        existing = self._members.get(claim_name)
        if existing is None:
            # Deleted before this handle ever saw an ADDED/MODIFIED event for it.
            return None
        lost_member = existing.model_copy(
            update={"lost": True, "ready": False, "pod_ips": (), "service_fqdn": None}
        )
        self._members[claim_name] = lost_member
        self._on_fill_change(claim_name, lost_member, existing)
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

    def note_error(self, error: Exception) -> None:
        """Records a sticky error for ``err()``."""
        if self._error is None:
            self._error = error

    def error(self) -> Exception | None:
        return self._error

    def _is_fill(self, claim_name: str, member: Member) -> bool:
        ordinal = self._ordinals.get(claim_name)
        return ordinal is not None and ordinal < self.size and member.warmpool in self._group_by_pool

    def _on_fill_change(self, claim_name: str, member: Member, previous: Member | None) -> None:
        if not self._is_fill(claim_name, member):
            return
        pool = member.warmpool
        was_counted = previous is not None and previous.ready and claim_name not in self._unable
        if claim_name not in self._unable and (member.terminal or member.lost):
            self._unable.add(claim_name)
            if member.lost:
                self._lost[pool] += 1
            else:
                self._failed[pool] += 1
            self._waiting[pool].pop(claim_name, None)
            event_type = BatchEventType.MEMBER_LOST if member.lost else BatchEventType.MEMBER_FAILED
            self._events.append(BatchEvent(type=event_type, member=member))
        elif claim_name not in self._unable and member.ready:
            if claim_name not in self._dispatched:
                self._route_ready(claim_name, pool)
        elif not member.ready:
            # A member handed out earlier stays handed out; one still waiting is dropped until
            # it is Ready again.
            self._waiting[pool].pop(claim_name, None)
        is_counted = member.ready and claim_name not in self._unable
        self._ready_count += int(is_counted) - int(was_counted)
        if self._mode == QUORUM_MODE:
            self._evaluate(pool)

    def _route_ready(self, claim_name: str, pool: str) -> None:
        verdict = self._verdicts.get(pool)
        if self._mode == STREAM_MODE or (verdict is not None and verdict.error is None):
            self._dispatch(claim_name)
        elif verdict is None:
            # In quorum mode, a group's Ready members are held until the group yields, so a
            # group that fails never hands out any of its members.
            self._waiting[pool][claim_name] = None

    def _dispatch(self, claim_name: str) -> None:
        self._dispatched.add(claim_name)
        self._events.append(
            BatchEvent(type=BatchEventType.MEMBER_READY, member=self._members[claim_name])
        )

    def _evaluate(self, pool: str) -> None:
        if pool in self._verdicts:
            return
        group = self._group_by_pool[pool]
        min_ready = group.min_ready or 0
        waiting = self._waiting[pool]
        if len(waiting) >= min_ready:
            names = list(waiting)
            waiting.clear()
            cohort = names[:min_ready]
            self._dispatched.update(cohort)
            self._set_verdict(GroupReady(warmpool=pool, members=[self._members[n] for n in cohort]))
            for name in names[min_ready:]:
                self._dispatch(name)
        elif group.size - self._failed[pool] - self._lost[pool] < min_ready:
            waiting.clear()
            self._set_verdict(GroupReady(warmpool=pool, error=QuorumUnreachableError(
                warmpool=pool,
                size=group.size,
                min_ready=min_ready,
                failed=self._failed[pool],
                lost=self._lost[pool],
            )))

    def _set_verdict(self, result: GroupReady) -> None:
        self._verdicts[result.warmpool] = result
        self._group_results.append(result)

    def _expire_undecided(self) -> None:
        for pool in self._group_by_pool:
            if pool not in self._verdicts:
                self._waiting[pool].clear()
                self._set_verdict(GroupReady(warmpool=pool, error=TimeoutError("Group quorum timed out")))

    def set_mode(self, mode: str) -> None:
        """Fixes how Ready members are handed out, on the first call of ``events()``
        (``STREAM_MODE``) or ``iter_ready_groups()`` (``QUORUM_MODE``).

        Raises:
            BatchError: If ``iter_ready_groups()`` is called after ``events()`` fixed stream mode.
        """
        if self._mode == mode or (self._mode == QUORUM_MODE and mode == STREAM_MODE):
            return
        if self._mode == STREAM_MODE:
            raise BatchError("iter_ready_groups() can't be used after events()")
        self._mode = mode
        if mode == STREAM_MODE:
            for waiting in self._waiting.values():
                for name in waiting:
                    self._dispatch(name)
                waiting.clear()
        else:
            for pool in self._group_by_pool:
                self._evaluate(pool)
            if self._fill_expired:
                self._expire_undecided()

    def record_create_failure(self, claim_name: str, warmpool: str, message: str) -> None:
        """Records a claim whose create failed as a terminal member with reason ``CreateFailed``."""
        if claim_name in self._members:
            # The watch has already seen the claim, so an earlier attempt did create it.
            return
        ordinal = parse_ordinal(self.batch_id, claim_name)
        if ordinal is not None:
            self._ordinals[claim_name] = ordinal
        member = Member(
            claim_name=claim_name,
            warmpool=warmpool,
            terminal=True,
            reason=CREATE_FAILED_REASON,
            message=message,
        )
        self._members[claim_name] = member
        self._on_fill_change(claim_name, member, None)

    def cancel_create(self, warmpool: str) -> None:
        """Counts a fill claim that won't be created because its group failed."""
        self._skipped += 1

    def group_failed(self, warmpool: str) -> bool:
        """Whether the group's quorum failed, so its remaining claims shouldn't be created."""
        verdict = self._verdicts.get(warmpool)
        return self._mode == QUORUM_MODE and verdict is not None and verdict.error is not None

    def expire_fill(self) -> bool:
        """Ends the fill at its deadline: in quorum mode, every group without a verdict gets a
        ``TimeoutError``. Members are left as they are.

        Returns ``False`` if the fill had already expired, so callers only notify on a change.
        """
        if self._fill_expired:
            return False
        self._fill_expired = True
        if self._mode == QUORUM_MODE:
            self._expire_undecided()
        return True

    def count_missing_fill_as_unable(self) -> None:
        """For a re-attached batch, counts each group's fill claims that don't exist as unable.
        The previous handle stopped creating before it detached, so they never will.
        """
        seen: dict[str, int] = dict.fromkeys(self._group_by_pool, 0)
        for name, member in self._members.items():
            if self._is_fill(name, member):
                seen[member.warmpool] += 1
        for pool, group in self._group_by_pool.items():
            missing = max(0, group.size - seen[pool])
            self._lost[pool] += missing
            self._missing += missing

    def note_lease_degraded(self) -> None:
        self._events.append(BatchEvent(type=BatchEventType.LEASE_DEGRADED))

    def pop_event(self) -> BatchEvent | None:
        return self._events.popleft() if self._events else None

    def pop_group_result(self) -> GroupReady | None:
        return self._group_results.popleft() if self._group_results else None

    def fill_settled(self) -> bool:
        """Whether every fill member is Ready or unable, or the fill deadline has passed."""
        resolved = self._ready_count + len(self._unable) + self._skipped + self._missing
        return self._fill_expired or resolved >= self.size

    def events_done(self) -> bool:
        return self.fill_settled() and not self._events

    def groups_done(self) -> bool:
        return len(self._verdicts) == len(self._group_by_pool) and not self._group_results
