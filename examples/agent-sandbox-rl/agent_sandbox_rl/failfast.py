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

"""Fail-fast watchdog for `SandboxFleet.acquire`.

The SDK's claim wait (`SandboxClient.create_sandbox`) watches the SandboxClaim
and returns when its Ready condition flips. It already fails fast on what the
claim itself can express: the claim was deleted, its template or pool is
missing, the controller reported a terminal reason. It cannot see the pod. A
claim whose pod sits in ``ImagePullBackOff`` or ``Unschedulable`` looks exactly
like one that is still coming up, and the wait runs out ``ready_timeout``
(15 minutes by default) before the caller learns anything.

`ClaimWatchdog` closes that gap from the fleet side without touching the SDK.
`acquire` names the claim itself, starts a watchdog on that name and blocks in
the SDK wait as before. The watchdog resolves the claim's pod and polls its
status; when `classify_pod` returns a verdict it records it and deletes the
claim, which ends the SDK wait with a "claim was deleted" error that `acquire`
converts into `SandboxStartError` carrying the real reason.

Cost: one small read per poll per in-flight claim, and nothing once the claim
is Ready (the watchdog is stopped on the success path). Warm adoptions are
Ready long before ``initial_delay_s`` elapses, so only cold starts are polled.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kubernetes import client

from . import constants

if TYPE_CHECKING:
  from .cluster import Cluster
  from .config import FailFastPolicy

logger = logging.getLogger(__name__)

# (connect, read) seconds for the watchdog's reads: a stalled API server must
# not pin a poll, and the next poll retries anyway.
_REQUEST_TIMEOUT = (3, 10)


@dataclass(frozen=True)
class StartVerdict:
  """Why a pod will not start. ``reason`` is the Kubernetes reason string."""
  reason: str
  message: str
  pod_name: str | None = None


def _as_dict(obj: Any) -> dict:
  """Accept a kubernetes model (``to_dict()``, snake_case keys) or a plain dict."""
  if obj is None:
    return {}
  if isinstance(obj, dict):
    return obj
  to_dict = getattr(obj, "to_dict", None)
  return to_dict() if callable(to_dict) else {}


def _get(d: dict, snake: str, camel: str):
  return d.get(snake) if snake in d else d.get(camel)


def classify_pod(pod: Any, *, now: float, first_seen: dict[str, float],
                 policy: "FailFastPolicy",
                 last_seen: dict[str, float] | None = None) -> StartVerdict | None:
  """Decide whether ``pod`` (a ``V1Pod``, its ``to_dict()`` or an API dict) is stuck.

  Pure apart from ``first_seen`` and ``last_seen``, which the caller keeps
  between polls: they map a condition key to the monotonic time it was first
  and last observed, so retryable reasons only count once they have persisted
  for the policy's grace period. All retryable waiting reasons of a container
  share one key: kubelet flips a failing pull between ``ErrImagePull`` and
  ``ImagePullBackOff`` on every retry, and that must not restart the grace. A
  key is dropped only after its condition has been absent for a full grace
  period, so a crash loop seen while the container is briefly running between
  restarts keeps its grace too. Without ``last_seen`` a key is dropped as soon
  as its condition is absent. Returns None while the pod may still come up.
  """
  if last_seen is None:
    last_seen = {}
  d = _as_dict(pod)
  meta = _as_dict(d.get("metadata"))
  status = _as_dict(d.get("status"))
  name = meta.get("name")
  present: set[str] = set()

  def _held_for(key: str, grace: float | None) -> float | None:
    if grace is None:                 # this class of reason is left to the timeout
      return None
    present.add(key)
    first = first_seen.setdefault(key, now)
    last_seen[key] = now
    held = now - first
    return held if held >= grace else None

  verdict: StartVerdict | None = None
  # A pod being deleted is not a verdict: the Sandbox controller recreates an
  # evicted or preempted pod under the same name, and a deleted claim or
  # Sandbox ends the SDK wait on its own.
  phase = status.get("phase")
  if phase in ("Failed", "Succeeded"):
    verdict = StartVerdict(
        f"Pod{phase}", status.get("message") or status.get("reason")
        or f"pod phase is {phase} before it became ready", name)

  statuses = list(_get(status, "init_container_statuses", "initContainerStatuses") or [])
  statuses += list(_get(status, "container_statuses", "containerStatuses") or [])
  for cs in statuses:
    if verdict is not None:
      break
    cs = _as_dict(cs)
    cname = cs.get("name") or "?"
    state = _as_dict(cs.get("state"))
    term = _as_dict(state.get("terminated"))
    last = _as_dict(_as_dict(_get(cs, "last_state", "lastState")).get("terminated"))
    # lastState describes the previous instance: an earlier OOM kill counts only
    # while the container is not running again. Test the value, not the key:
    # a V1Pod's to_dict() carries every state key, with None for the unset ones.
    if (term.get("reason") == "OOMKilled"
        or (last.get("reason") == "OOMKilled" and not state.get("running"))):
      verdict = StartVerdict(
          "OOMKilled", f"container '{cname}' was OOM-killed while starting; "
          "the template's memory limit is too small for this image", name)
      break
    waiting = _as_dict(state.get("waiting"))
    reason = waiting.get("reason")
    if not reason:
      continue
    msg = waiting.get("message") or reason
    if reason in constants.TERMINAL_WAITING_REASONS:
      verdict = StartVerdict(reason, f"container '{cname}': {msg}", name)
    elif reason in constants.RETRYABLE_WAITING_REASONS:
      held = _held_for(f"{cname}:retryable", policy.grace_s)
      if held is not None:
        verdict = StartVerdict(
            reason, f"container '{cname}' has been in {reason} for {int(held)}s: {msg}",
            name)

  if verdict is None:
    for cond in status.get("conditions") or []:
      cond = _as_dict(cond)
      if (cond.get("type") == "PodScheduled" and cond.get("status") == "False"
          and cond.get("reason") == "Unschedulable"):
        held = _held_for("Unschedulable", policy.unschedulable_grace_s)
        if held is not None:
          verdict = StartVerdict(
              "Unschedulable",
              f"pod unschedulable for {int(held)}s: {cond.get('message') or ''}".rstrip(": "),
              name)
        break

  for key in list(first_seen):
    if key in present:
      continue
    grace = (policy.unschedulable_grace_s if key == "Unschedulable"
             else policy.grace_s)
    if grace is None or now - last_seen.get(key, float("-inf")) >= grace:
      del first_seen[key]
      last_seen.pop(key, None)
  return verdict


class ClaimWatchdog(threading.Thread):
  """Poll a claim's pod while `acquire` waits on the claim; on a terminal state
  record the verdict and delete the claim so the wait ends now.

  Best-effort by design: every API error is swallowed at debug level and the
  next poll retries, because a watchdog that raises would take a healthy claim
  down with it. ``stop()`` is cheap (sets a flag, no join) and returns the
  verdict if one was reached.
  """

  def __init__(self, cluster: "Cluster", claim_name: str, policy: "FailFastPolicy"):
    super().__init__(name=f"asrl-failfast-{claim_name}", daemon=True)
    self.cluster = cluster
    self.claim_name = claim_name
    self.policy = policy
    self.verdict: StartVerdict | None = None
    self.pod_name: str | None = None
    self._stop_evt = threading.Event()
    # Serialises the stop-versus-delete decision. `stop()` and the verdict path
    # both take it, so once `stop()` has returned the watchdog can no longer
    # delete the claim, and a verdict committed first is what `stop()` returns.
    self._decide_lock = threading.Lock()
    self.deleted_claim = False
    self._first_seen: dict[str, float] = {}
    self._last_seen: dict[str, float] = {}

  def stop(self) -> StartVerdict | None:
    """Stop polling. Blocks while a verdict is being committed, so on return
    either ``verdict`` is set and the claim delete has been attempted
    (``deleted_claim`` says whether it succeeded), or no delete will happen."""
    with self._decide_lock:
      self._stop_evt.set()
      return self.verdict

  def run(self) -> None:
    if self._stop_evt.wait(self.policy.initial_delay_s):
      return
    while not self._stop_evt.is_set():
      verdict = None
      try:
        verdict = self._check()
      except Exception:  # noqa: BLE001 — best-effort; never take the claim down
        logger.debug("fail-fast watchdog: check failed for claim %s",
                     self.claim_name, exc_info=True)
      if verdict is not None:
        with self._decide_lock:
          if self._stop_evt.is_set():
            return                       # acquire() already owns the outcome
          self.verdict = verdict
          logger.error("claim %s will not start (%s on pod %s): %s; deleting the claim",
                       self.claim_name, verdict.reason, verdict.pod_name, verdict.message)
          try:
            # Bounded: this runs under the decision lock that `stop()` takes,
            # so an unbounded delete would pin acquire()'s success path too.
            self.cluster.resources.delete_claim(
                self.claim_name, request_timeout=_REQUEST_TIMEOUT)
            self.deleted_claim = True
          except Exception:  # noqa: BLE001
            logger.warning("fail-fast watchdog: failed to delete claim %s",
                           self.claim_name, exc_info=True)
        return
      if self._stop_evt.wait(self.policy.poll_s):
        return

  # --- one poll ---------------------------------------------------------- #
  def _check(self) -> StartVerdict | None:
    if self.pod_name is None:
      self.pod_name = self._resolve_pod_name()
      if self.pod_name is None:
        return None                      # claim not bound to a sandbox yet
    pod = self._read_pod(self.pod_name)
    if pod is None:
      return None                        # pod not created yet; the claim watch owns "gone"
    return classify_pod(pod, now=time.monotonic(), first_seen=self._first_seen,
                        last_seen=self._last_seen,
                        policy=self.policy)

  def _resolve_pod_name(self) -> str | None:
    return resolve_pod_name_for_claim(self.cluster, self.claim_name)

  def _read_pod(self, name: str):
    return read_pod_status(self.cluster, name)


def resolve_pod_name_for_sandbox(cluster: "Cluster", sandbox_name: str) -> str:
  """Resolve the pod backing ``sandbox_name`` (via ``POD_NAME_ANNOTATION`` when set)."""
  ns = cluster.namespace
  api = cluster.custom_api
  try:
    sandbox = api.get_namespaced_custom_object(
        group=constants.SANDBOX_GROUP, version=constants.SANDBOX_VERSION,
        namespace=ns, plural=constants.SANDBOXES_PLURAL, name=sandbox_name,
        _request_timeout=_REQUEST_TIMEOUT)
  except client.ApiException as e:
    if e.status != 404:
      raise
    return sandbox_name
  if not isinstance(sandbox, dict):
    return sandbox_name
  meta = sandbox.get("metadata")
  annotations = (meta.get("annotations") if isinstance(meta, dict) else None) or {}
  pod_name = (
      annotations.get(constants.POD_NAME_ANNOTATION)
      if isinstance(annotations, dict) else None
  )
  return pod_name if isinstance(pod_name, str) and pod_name else sandbox_name


def resolve_pod_name_for_claim(cluster: "Cluster", claim_name: str) -> str | None:
  """Resolve the pod backing ``claim_name`` once the controller binds a Sandbox."""
  ns = cluster.namespace
  api = cluster.custom_api
  try:
    claim = api.get_namespaced_custom_object(
        group=constants.GROUP, version=constants.VERSION, namespace=ns,
        plural=constants.CLAIMS_PLURAL, name=claim_name,
        _request_timeout=_REQUEST_TIMEOUT)
  except client.ApiException as e:
    if e.status == 404:
      return None
    raise
  if not isinstance(claim, dict):
    return None
  status = claim.get("status")
  sb = (status.get("sandbox") if isinstance(status, dict) else None) or {}
  if not isinstance(sb, dict):
    return None
  sandbox_name = sb.get("name") or sb.get("Name")
  if not isinstance(sandbox_name, str) or not sandbox_name:
    return None
  return resolve_pod_name_for_sandbox(cluster, sandbox_name)


def read_pod_status(cluster: "Cluster", name: str):
  """Read pod status from ``cluster.core_api`` with bounded timeout (None on 404)."""
  try:
    return cluster.core_api.read_namespaced_pod_status(
        name, cluster.namespace, _request_timeout=_REQUEST_TIMEOUT)
  except client.ApiException as e:
    if e.status == 404:
      return None
    raise


class BatchWatchdog(threading.Thread):
  """Poll cold-starting pending members of one `SandboxBatch`.

  Warm adoptions reach ``READY`` in under a second, long before
  ``policy.initial_delay_s``, so `BatchWatchdog` inspects only active in-flight
  waiters from ``batch._acquire_waiters`` via ``batch.get_member()`` in memory
  and performs zero Kubernetes ``GET``s on warm runs and ``O(1)`` idle polls
  when no acquires are in flight. When a member remains ``PENDING`` past
  ``initial_delay_s`` and `classify_pod` returns a `StartVerdict`,
  `BatchWatchdog` records the verdict before calling
  ``batch.release_member(member)`` to wake the blocked `acquire()` call.
  """

  def __init__(
      self,
      cluster: "Cluster",
      batch: Any,
      policy: "FailFastPolicy",
      record_verdict: Any,
      *,
      loop: asyncio.AbstractEventLoop | None = None,
  ) -> None:
    super().__init__(
        name=f"asrl-batch-failfast-{getattr(batch, 'batch_id', 'batch')}",
        daemon=True,
    )
    self.cluster = cluster
    self.batch = batch
    self.policy = policy
    self._record_verdict = record_verdict
    self._loop = loop
    self._stop_evt = threading.Event()
    self._decide_lock = threading.Lock()
    self._in_flight_acquires = 0
    self._last_acquire_at = 0.0
    self._first_seen_at: dict[str, float] = {}
    self._pod_names: dict[str, str] = {}
    self._cond_first_seen: dict[str, dict[str, float]] = {}
    self._cond_last_seen: dict[str, dict[str, float]] = {}

  def enter_acquire(self, started_at: float | None = None) -> None:
    ts = time.monotonic() if started_at is None else started_at
    with self._decide_lock:
      self._in_flight_acquires += 1
      self._last_acquire_at = ts

  def exit_acquire(self, claim_name: str | None = None) -> None:
    with self._decide_lock:
      self._in_flight_acquires = max(0, self._in_flight_acquires - 1)
      if claim_name is not None:
        self._prune(claim_name)
      if self._in_flight_acquires == 0:
        self._first_seen_at.clear()
        self._pod_names.clear()
        self._cond_first_seen.clear()
        self._cond_last_seen.clear()

  def stop(self) -> None:
    self._stop_evt.set()

  def _prune(self, claim_name: str) -> None:
    self._first_seen_at.pop(claim_name, None)
    self._pod_names.pop(claim_name, None)
    self._cond_first_seen.pop(claim_name, None)
    self._cond_last_seen.pop(claim_name, None)

  @staticmethod
  def _is_pending(member: Any) -> bool:
    if member is None:
      return False
    state = getattr(member, "state", None)
    return getattr(state, "value", state) == "pending"

  @staticmethod
  def _batch_session_open(batch: Any) -> bool:
    client_obj = getattr(batch, "_client", None)
    helper = getattr(client_obj, "k8s_helper", None) if client_obj is not None else None
    api = (
        (
            getattr(helper, "_api_client", None)
            or getattr(getattr(helper, "custom_objects_api", None), "api_client", None)
        )
        if helper is not None
        else None
    )
    if api is None:
      return True
    pm = getattr(getattr(api, "rest_client", None), "pool_manager", None)
    return getattr(pm, "closed", False) is not True

  @staticmethod
  def _has_waiter(waiters: Any, b_lock: Any, claim_name: str) -> bool:
    if not isinstance(waiters, dict):
      return True
    if b_lock is not None and hasattr(b_lock, "__enter__"):
      with b_lock:
        return claim_name in waiters
    return claim_name in waiters

  def run(self) -> None:
    while not self._stop_evt.is_set():
      if (
          self.batch.err() is not None
          or getattr(self.batch, "released", False) is True
          or getattr(self.batch, "_released", False) is True
          or getattr(self.batch, "_detached", False) is True
      ):
        return
      sleep_s = self.policy.poll_s
      try:
        sleep_s = self._poll_once()
      except Exception:  # noqa: BLE001 (best-effort; never disrupt healthy claims)
        logger.debug("batch fail-fast watchdog: poll failed for batch %s",
                     getattr(self.batch, "batch_id", "?"), exc_info=True)
      if self._stop_evt.wait(sleep_s):
        return

  def _poll_once(self) -> float:
    with self._decide_lock:
      if self._in_flight_acquires <= 0 and not self._first_seen_at:
        return self.policy.poll_s
      tracked = set(self._first_seen_at)
      in_flight = self._in_flight_acquires
      last_acq = self._last_acquire_at

    waiters = getattr(self.batch, "_acquire_waiters", None)
    b_lock = getattr(self.batch, "_lock", None)
    if in_flight > 0 and isinstance(waiters, dict):
      if b_lock is not None and hasattr(b_lock, "__enter__"):
        with b_lock:
          tracked.update(waiters.keys())
      else:
        while True:
          try:
            tracked.update(tuple(waiters))
            break
          except RuntimeError:
            continue

    now = time.monotonic()
    if not tracked:
      if in_flight > 0 and now - last_acq < 0.5:
        return min(self.policy.poll_s, 0.02)
      return self.policy.poll_s

    earliest_due: float | None = None
    for claim_name in tracked:
      if self._stop_evt.is_set():
        return self.policy.poll_s
      try:
        member = self.batch.get_member(claim_name)
      except Exception:  # noqa: BLE001
        with self._decide_lock:
          self._prune(claim_name)
        continue
      if (
          not self._is_pending(member)
          or not self._has_waiter(waiters, b_lock, claim_name)
      ):
        with self._decide_lock:
          self._prune(claim_name)
        continue
      with self._decide_lock:
        if (
            claim_name not in self._first_seen_at
            and isinstance(waiters, dict)
            and self._in_flight_acquires <= 0
        ):
          continue
        first = self._first_seen_at.setdefault(claim_name, now)
      elapsed = now - first
      if elapsed < self.policy.initial_delay_s:
        rem = self.policy.initial_delay_s - elapsed
        earliest_due = rem if earliest_due is None else min(earliest_due, rem)
        continue
      try:
        verdict = self._check_member(claim_name, member, now=now)
      except Exception:  # noqa: BLE001 (isolated per member)
        logger.debug("batch fail-fast watchdog: check failed for claim %s",
                     claim_name, exc_info=True)
        continue
      if verdict is None:
        continue
      if not self._has_waiter(waiters, b_lock, claim_name):
        with self._decide_lock:
          self._prune(claim_name)
        continue
      try:
        cur = self.batch.get_member(claim_name)
      except Exception:  # noqa: BLE001
        cur = None
      if not self._is_pending(cur):
        with self._decide_lock:
          self._prune(claim_name)
        continue
      should_release = False
      with self._decide_lock:
        if self._stop_evt.is_set():
          return self.policy.poll_s
        if claim_name in self._first_seen_at:
          self._record_verdict(claim_name, verdict)
          self._prune(claim_name)
          should_release = True
        else:
          self._prune(claim_name)
      if not should_release:
        continue
      logger.error(
          "batch claim %s will not start (%s on pod %s): %s; releasing member",
          claim_name, verdict.reason, verdict.pod_name, verdict.message)
      try:
        rel_fn = self.batch.release_member
        if inspect.iscoroutinefunction(rel_fn):
          if (
              self._loop is not None
              and not self._loop.is_closed()
              and self._loop.is_running()
              and self._batch_session_open(self.batch)
          ):
            asyncio.run_coroutine_threadsafe(rel_fn(cur), self._loop).result(timeout=15.0)
          else:
            state = getattr(self.batch, "_state", None)
            try:
              if state is not None:
                state.release_member(claim_name)
              wake = getattr(self.batch, "_wake_acquire", None)
              if callable(wake):
                wake(claim_name)
            except Exception:  # noqa: BLE001
              pass
            self.cluster.resources.delete_claim(
                claim_name, request_timeout=_REQUEST_TIMEOUT)
        else:
          rel_fn(cur)
      except Exception as exc:  # noqa: BLE001
        if any(b.__name__ == "BatchError" for b in type(exc).__mro__) or not self._batch_session_open(self.batch):
          try:
            self.cluster.resources.delete_claim(
                claim_name, request_timeout=_REQUEST_TIMEOUT)
          except Exception:  # noqa: BLE001
            logger.warning("batch fail-fast watchdog: failed to delete claim %s",
                           claim_name, exc_info=True)
        else:
          logger.warning("batch fail-fast watchdog: failed to release member %s",
                         claim_name, exc_info=True)

    if earliest_due is not None:
      return min(self.policy.poll_s, max(0.0, earliest_due))
    return self.policy.poll_s

  def _check_member(
      self, claim_name: str, member: Any, *, now: float
  ) -> StartVerdict | None:
    with self._decide_lock:
      if claim_name not in self._first_seen_at:
        return None
      pod_name = self._pod_names.get(claim_name)
    pod = None
    if pod_name is None:
      sb_name = getattr(member, "sandbox_name", None)
      if not isinstance(sb_name, str) or not sb_name:
        # The batch's shared watch populates member.sandbox_name once bound;
        # avoid polling GET SandboxClaim redundantly.
        return None
      # Cold-born Sandboxes name their Pod identically to sandbox_name, so try
      # Pod/status directly before falling back to GET Sandbox for adopted pods
      # carrying POD_NAME_ANNOTATION.
      pod = read_pod_status(self.cluster, sb_name)
      if pod is not None:
        pod_name = sb_name
      else:
        resolved = resolve_pod_name_for_sandbox(self.cluster, sb_name)
        if resolved == sb_name:
          return None
        pod_name = resolved
        pod = read_pod_status(self.cluster, pod_name)
      with self._decide_lock:
        if claim_name not in self._first_seen_at:
          return None
        self._pod_names[claim_name] = pod_name
      if pod is None:
        return None
    else:
      pod = read_pod_status(self.cluster, pod_name)
      if pod is None:
        return None
    with self._decide_lock:
      if claim_name not in self._first_seen_at:
        return None
      first_seen = dict(self._cond_first_seen.get(claim_name, {}))
      last_seen = dict(self._cond_last_seen.get(claim_name, {}))
    verdict = classify_pod(
        pod,
        now=now,
        first_seen=first_seen,
        last_seen=last_seen,
        policy=self.policy,
    )
    with self._decide_lock:
      if claim_name not in self._first_seen_at:
        return None
      self._cond_first_seen[claim_name] = first_seen
      self._cond_last_seen[claim_name] = last_seen
    return verdict
