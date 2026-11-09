"""Mutant harness for the Pipeline 8 performance suite.

Copies a tree that has the candidate performance fixes applied and injects one realistic
regression per mutant (a Python or C-level scan on a per-item path, an extra request, retained
memory, a poll, deferred work, a failure-path wakeup, periodic housekeeping, and so on). Each
mutant must turn at least one gate red: the SDK perf file, the RL perf file, or Check 8
(``verify_batch_static.py --check HOT_PATH_COMPLEXITY``).

Usage: python perf_mutants.py <fixed_tree> <out_dir> [mutant ...]
Creates <out_dir>/<mutant>/ trees, each with its single injected regression; naming mutants
builds only those.
"""

import pathlib
import shutil
import sys

SRC = pathlib.Path(sys.argv[1])
OUT = pathlib.Path(sys.argv[2])
P = "clients/python/agentic-sandbox-client/k8s_agent_sandbox"
R = "examples/agent-sandbox-rl/agent_sandbox_rl"


def insert_after(text: str, anchor: str, line: str, nth: int = 0) -> str:
    lines = text.split("\n")
    hits = [i for i, value in enumerate(lines) if value.strip() == anchor.strip()]
    assert len(hits) > nth, (anchor, len(hits))
    lines.insert(hits[nth] + 1, line)
    return "\n".join(lines)


MUTANTS = {
    # Python-level O(N) scan in an O(1) transition.
    "py_scan_upsert": (f"{P}/batch_state.py", lambda s: insert_after(
        s, "previous = self._members.get(claim_name)",
        "        _stale = [n for n in self._members if n == claim_name]")),
    # C-level copy in an O(1) transition.
    "c_copy_upsert": (f"{P}/batch_state.py", lambda s: insert_after(
        s, "previous = self._members.get(claim_name)",
        "        _snap = dict(self._members)")),
    # C-level sort per watch event in the handle.
    "c_sort_watch": (f"{P}/sandbox_batch.py", lambda s: insert_after(
        s, 'claim_name = (obj.get("metadata") or {}).get("name", "")',
        "                    _s = sorted(self._connected)")),
    # Extra RPC on connect() cache hit.
    "rpc_connect": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            if cached is not None:\n                return cached[0]\n",
        "            if cached is not None:\n"
        "                self._client.k8s_helper.get_sandbox(member.claim_name, self.namespace)\n"
        "                return cached[0]\n", 1)),
    # Extra RPC per watch event.
    "rpc_watch": (f"{P}/sandbox_batch.py", lambda s: insert_after(
        s, 'claim_name = (obj.get("metadata") or {}).get("name", "")',
        "                    self._client.k8s_helper.get_sandbox_claim(claim_name, self.namespace)")),
    # Extra RPC in still_reaches.
    "rpc_still_reaches": (f"{R}/batch_claims.py", lambda s: s.replace(
        "    current = self.batch.get_member(self.member.claim_name)\n",
        "    self.cluster.custom_api.get_namespaced_custom_object()\n"
        "    current = self.batch.get_member(self.member.claim_name)\n", 1)),
    # Reference-sized leak per release.
    "ref_leak_release": (f"{P}/batch_state.py", lambda s: insert_after(
        s, "self._released.add(claim_name)",
        "        self.__dict__.setdefault('_leak', []).append(None)")),
    # C-level copy under the fleet lock.
    "c_copy_release": (f"{R}/fleet.py", lambda s: s.replace(
        "      if self._handles.pop(id(handle), None) is None:\n",
        "      _all = list(self._handles.values())\n"
        "      if self._handles.pop(id(handle), None) is None:\n", 1)),
    # Round 4: an O(G) scan replacing the O(1) short-of-quorum check.
    "quorum_scan_g": (f"{P}/batch_state.py", lambda s: s.replace(
        "        if self._groups_short_of_quorum:\n            return\n",
        "        if any(len(self._waiting[p]) < self._min_ready[p] for p in self._min_ready):\n"
        "            return\n", 1)),
    # Round 4: O(K log K) sort of one group per Ready fill event.
    "fill_sort_pool": (f"{P}/batch_state.py", lambda s: s.replace(
        "                if outcome is None:\n                    self._waiting[pool][claim_name] = None\n",
        "                if outcome is None:\n                    _order = sorted(self._waiting[pool])\n"
        "                    self._waiting[pool][claim_name] = None\n", 1)),
    # Round 4: O(R) copy of release history per release.
    "release_copy_released": (f"{P}/batch_state.py", lambda s: insert_after(
        s, "self._released.add(claim_name)", "        _snap = frozenset(self._released)")),
    # Round 4: non-allocating O(G) C walk per event.
    "c_max_min_ready": (f"{P}/batch_state.py", lambda s: insert_after(
        s, "previous = self._members.get(claim_name)",
        "        _f = max(self._min_ready.values(), default=0)")),
    # Round 5: non-allocating C walk over one pool's slice per fill event.
    "sub_cscan_waiting": (f"{P}/batch_state.py", lambda s: s.replace(
        "        pool = member.warmpool\n        if claim_name not in self._unable and member.state in",
        "        pool = member.warmpool\n        _m = max(self._waiting[pool], default=None)\n"
        "        if claim_name not in self._unable and member.state in", 1)),
    # Round 5: non-allocating C walk through .get() on a per-pool index.
    "get_cscan_pool": (f"{P}/batch_state.py", lambda s: insert_after(
        s, "previous = self._members.get(claim_name)",
        "        _m = max(self._members_by_pool.get(member.warmpool, {}), default=None)")),
    # Round 5: non-allocating C walk through a local alias.
    "alias_cscan_members": (f"{P}/batch_state.py", lambda s: insert_after(
        s, "previous = self._members.get(claim_name)",
        "        members = self._members\n        _m = max(members)")),
    # Round 5: an extra GET on every RL acquire.
    "rl_rpc_acquire": (f"{R}/fleet.py", lambda s: s.replace(
        "    fam = repo_family(task)\n",
        "    cluster.resources.get_warmpool(pool)\n    fam = repo_family(task)\n", 1)),
    # Round 5: one leaked Event per acquire on the handle (caught by test_sandbox_batch.py).
    "handle_waiter_leak": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                if member is not None:\n"
        "                    self._acquire_waiters.pop(claim_name, None)\n"
        "                    return member\n",
        "                if member is not None:\n                    return member\n", 1)),
    # Round 6: an O(H) scan of live handles per fleet acquire.
    "fleet_acquire_scan": (f"{R}/fleet.py", lambda s: s.replace(
        "        self._handles[id(handle)] = handle\n",
        "        if any(h.claim_name == handle.claim_name for h in self._handles.values()):\n"
        "          raise RuntimeError('duplicate')\n"
        "        self._handles[id(handle)] = handle\n", 1)),
    # Round 6: an extra claim GET per member release.
    "sdk_delete_get": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                    self._client.k8s_helper.delete_sandbox_claim(\n",
        "                    self._client.k8s_helper.get_sandbox_claim(claim_name, self.namespace)\n"
        "                    self._client.k8s_helper.delete_sandbox_claim(\n", 1)),
    # Round 6: re-check the pool on every acquire. Forcing need_check alone is a no-op, since
    # _check_pool returns early for a checked pool, so the cache check goes too.
    "sdk_acquire_recheck": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "need_check = warmpool not in self._checked_pools", "need_check = True", 1).replace(
        "                if warmpool in self._checked_pools:\n                    return\n"
        "                if now >= deadline:\n",
        "                if now >= deadline:\n", 1)),
    # Round 7: an O(K) scan of the pool per handle acquire.
    "sdk_acquire_members": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            acquire_seq = self._state.enter_acquire(warmpool)\n",
        "            acquire_seq = self._state.enter_acquire(warmpool)\n"
        "            _pending = [m for m in self._state.members(warmpool) if m.state is MemberState.PENDING]\n",
        1)),
    # Round 7: sweep every cached connection per release_member.
    "release_sweep_stale": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "        self._close_cached_connections(\"release_member\", [member.claim_name])\n",
        "        with self._lock:\n"
        "            _stale = self._pop_stale_connections(list(self._connected))\n"
        "        self._close_connections(_stale, \"release_member\")\n"
        "        self._close_cached_connections(\"release_member\", [member.claim_name])\n", 1)),
    # Round 7: wake every waiting acquire per release.
    "release_notify_all": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            needs_delete = self._state.release_member(member.claim_name)\n"
        "            self._wake_acquire(member.claim_name)\n",
        "            needs_delete = self._state.release_member(member.claim_name)\n"
        "            self._notify_all()\n", 1)),
    # Round 7: non-allocating walk in a per-event BatchState method outside the old O(1) set.
    "exhausted_cscan": (f"{P}/batch_state.py", lambda s: s.replace(
        "    def take_exhausted_pools(self) -> set[str]:\n",
        "    def take_exhausted_pools(self) -> set[str]:\n"
        "        _m = max(self._members, default=None)\n", 1)),
    # Round 7: an extra request per batch-mode BatchClaim.release.
    "rl_batchclaim_release_get": (f"{R}/batch_claims.py", lambda s: s.replace(
        "      self.batch.release_member(self.member)\n",
        "      self.cluster.resources.get_warmpool(self.member.warmpool)\n"
        "      self.batch.release_member(self.member)\n", 1)),
    # Round 8: a copy of the resolved set on every consumer wake.
    "fill_settled_copy": (f"{P}/batch_state.py", lambda s: s.replace(
        "        return self._fill_expired or len(self._resolved) + self._never_created >= self.size\n",
        "        return self._fill_expired or len(set(self._resolved)) + self._never_created >= self.size\n",
        1)),
    # Round 8: an O(K) count per fill create.
    "group_failed_scan": (f"{P}/batch_state.py", lambda s: s.replace(
        "        if self._mode == ConsumerMode.QUORUM:\n            return self._quorum_error is not None\n",
        "        _n = sum(1 for _ in self._members_by_pool.get(warmpool, ()))\n"
        "        if self._mode == ConsumerMode.QUORUM:\n            return self._quorum_error is not None\n",
        1)),
    # Round 8: a dedupe scan of every cached connection per connect() miss.
    "connect_miss_scan": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            pod_ip = select_pod_ip(current.pod_ips)\n",
        "            pod_ip = select_pod_ip(current.pod_ips)\n"
        "            _dupes = [c for c in self._connected.values() if c[1] == pod_ip]\n", 1)),
    # Round 8: one retained reference per handle release.
    "handle_release_leak": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            needs_delete = self._state.release_member(member.claim_name)\n",
        "            needs_delete = self._state.release_member(member.claim_name)\n"
        "            self.__dict__.setdefault('_leak', []).append(None)\n", 1)),
    # Round 8: an O(K) snapshot per RL batch-mode acquire.
    "rl_batch_acquire_members": (f"{R}/batch_claims.py", lambda s: s.replace(
        "    batch = self._batch_for(cluster, pool)\n    started = time.monotonic()\n",
        "    batch = self._batch_for(cluster, pool)\n"
        "    logger.debug(\"pool size %d\", len(batch.members(pool)))\n"
        "    started = time.monotonic()\n", 1)),
    # Round 8: an extra request per RL batch-mode acquire.
    "rl_batch_acquire_get": (f"{R}/batch_claims.py", lambda s: s.replace(
        "    batch = self._batch_for(cluster, pool)\n    started = time.monotonic()\n",
        "    batch = self._batch_for(cluster, pool)\n"
        "    cluster.resources.get_warmpool(pool)\n"
        "    started = time.monotonic()\n", 1)),
    # Round 8: a non-allocating walk of cached connections per release.
    "release_member_cwalk": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            needs_delete = self._state.release_member(member.claim_name)\n",
        "            needs_delete = self._state.release_member(member.claim_name)\n"
        "            _m = max(self._connected, default=None)\n", 1)),
    # Round 9: rebuild the waiter table per finished acquire (O(W) with W concurrent acquires).
    "waiters_rebuild": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                if member is not None:\n"
        "                    self._acquire_waiters.pop(claim_name, None)\n",
        "                if member is not None:\n"
        "                    self._acquire_waiters = {\n"
        "                        k: v for k, v in self._acquire_waiters.items() if k != claim_name\n"
        "                    }\n", 1)),
    # Round 9: count the group's remaining planned creates per fill create.
    "fill_plan_scan": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                claim_name, group = self._create_plan.popleft()\n",
        "                claim_name, group = self._create_plan.popleft()\n"
        "                _left = sum(1 for _n, g in self._create_plan if g.warmpool == group.warmpool)\n",
        1)),
    # Round 9: remember every resourceVersion the watch saw, forever.
    "watch_rv_leak": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                    if seen_rv:\n                        rv = seen_rv\n",
        "                    if seen_rv:\n                        rv = seen_rv\n"
        "                        self.__dict__.setdefault('_claim_rvs', {})[id(obj)] = seen_rv\n",
        1)),
    # Blind auditor: an unbounded set deduping watch events by resourceVersion.
    "watch_seen_rvs": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                    if seen_rv:\n                        rv = seen_rv\n",
        "                    if seen_rv:\n                        rv = seen_rv\n"
        "                        _seen = self.__dict__.setdefault('_seen_rvs', set())\n"
        "                        if seen_rv in _seen:\n                            continue\n"
        "                        _seen.add(seen_rv)\n", 1)),
    # Blind auditor: a pool snapshot per batch-mode fleet acquire.
    "fleet_batch_members": (f"{R}/fleet.py", lambda s: s.replace(
        "          pod_ip = select_pod_ip(claim.member.pod_ips)\n",
        "          current = next((m for m in claim.batch.members(pool)\n"
        "                          if m.sandbox_name == claim.member.sandbox_name), claim.member)\n"
        "          pod_ip = select_pod_ip(current.pod_ips)\n", 1)),
    # Blind auditor: a blocked acquire polls every 50 ms instead of sleeping until woken.
    "acquire_poll": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "waiter.wait(wait_timeout)", "waiter.wait(min(wait_timeout, 0.2))", 1)),
    # Round 10: a scan of the checked pools per acquire.
    "pool_scan": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "need_check = warmpool not in self._checked_pools",
        "need_check = not any(p == warmpool for p in self._checked_pools)", 1)),
    # Round 10: the plan looked up by a linear scan per acquire.
    "plan_scan": (f"{R}/fleet.py", lambda s: s.replace(
        "    return self._by_image.get(image)\n",
        "    return next((e for e in self.entries if e.image == image), None)\n", 1)),
    # Round 10: an idle consumer polls every 200 ms.
    "next_poll": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                self._changed.wait(timeout)\n",
        "                self._changed.wait(min(timeout, 0.2))\n", 1)),
    # Round 10: watches that end 10x sooner, so 10x the watch requests.
    "watch_timeout": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "_WATCH_TIMEOUT_SECONDS = 30\n", "_WATCH_TIMEOUT_SECONDS = 3\n", 1)),
    # Round 10: a fresh ApiClient per exec.
    "exec_nocache": (f"{R}/cluster.py", lambda s: s.replace(
        "    core = getattr(self._exec_local, \"core_api\", None)\n",
        "    core = None\n", 1)),
    # Round 10 (B): sweep every cached connection on each DELETED event.
    "deleted_sweep": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                        stale = self._pop_stale_connections([claim_name])\n",
        "                        stale = self._pop_stale_connections(\n"
        "                            list(self._connected) if event_type == \"DELETED\" else [claim_name]\n"
        "                        )\n", 1)),
    # Round 10 (B): a blocked acquire and idle consumers poll once a second.
    "poll_1hz": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "waiter.wait(wait_timeout)", "waiter.wait(min(wait_timeout, 1.0))", 1).replace(
        "                self._changed.wait(timeout)\n",
        "                self._changed.wait(min(timeout, 1.0))\n", 1)),
    # Round 10 (B): on-demand keys kept in a list.
    "ondemand_list": (f"{R}/fleet.py", lambda s: s.replace(
        "    self._ondemand: set[tuple[str, str]] = set()",
        "    self._ondemand: list[tuple[str, str]] = []", 1).replace(
        "          self._ondemand.add(key)\n", "          self._ondemand.append(key)\n", 1).replace(
        "          self._ondemand.discard(key)\n",
        "          if key in self._ondemand:\n            self._ondemand.remove(key)\n", 1)),
    # Round 10 (B): checked pools kept in a list.
    "checked_pools_list": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "        self._checked_pools: set[str] = {g.warmpool for g in state.groups}\n",
        "        self._checked_pools: list[str] = [g.warmpool for g in state.groups]\n", 1).replace(
        "self._checked_pools.add(", "self._checked_pools.append(")),
    # Round 11 (B): the handle's by-name lookup walks a members() snapshot.
    "handle_get_member_filter": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            return self._state.get_member(claim_name)\n",
        "            return next(filter(lambda m: m.claim_name == claim_name, self._state.members()),"
        " None)\n", 1)),
    "async_get_member_filter": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "        return self._state.get_member(claim_name)\n",
        "        return next(filter(lambda m: m.claim_name == claim_name, self._state.members()),"
        " None)\n", 1)),
    # Round 11: err() snapshots every member.
    "err_scan": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "        with self._lock:\n            return self._state.error()\n",
        "        with self._lock:\n            self._state.members()\n"
        "            return self._state.error()\n", 1)),
    # Round 11 (B): blocked acquires and idle consumers poll every 2 s.
    "poll_2s": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "waiter.wait(wait_timeout)", "waiter.wait(min(wait_timeout, 2.0))", 1).replace(
        "                self._changed.wait(timeout)\n",
        "                self._changed.wait(min(timeout, 2.0))\n", 1)),
    "async_poll_2s": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "await asyncio.wait_for(waiter.wait(), wait_timeout)",
        "await asyncio.wait_for(waiter.wait(), min(wait_timeout, 2.0))", 1).replace(
        "await asyncio.wait_for(self._changed.wait(), timeout)",
        "await asyncio.wait_for(self._changed.wait(), min(timeout, 2.0))", 1)),
    # Round 11 (B): an eager Sandbox read per acquire, as a log argument.
    "rl_eager_name_hash": (f"{R}/fleet.py", lambda s: insert_after(
        s, "pod = sandbox.get_pod_name()",
        "          logger.debug(\"acquired %s\", sandbox.get_sandbox_name_hash())")),
    # Round 11 (A): a DELETED event wakes every waiting acquire.
    "deleted_notify_all": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                            if self._state.mark_lost(claim_name) is not None:\n",
        "                            self._notify_all()\n"
        "                            if self._state.mark_lost(claim_name) is not None:\n", 1)),
    # Round 11 (A): a DELETED event re-reads the claim.
    "deleted_get": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                            if self._state.mark_lost(claim_name) is not None:\n",
        "                            self._client.k8s_helper.get_sandbox_claim(claim_name, self.namespace)\n"
        "                            if self._state.mark_lost(claim_name) is not None:\n", 1)),
    # Round 11 (A): BatchClaims keeps every claim it hands out.
    "rl_claims_keep": (f"{R}/batch_claims.py", lambda s: s.replace(
        "    return BatchClaim(\n        batch=batch, member=member, expires_at=expires_at, cluster=cluster)\n",
        "    claim = BatchClaim(\n        batch=batch, member=member, expires_at=expires_at, cluster=cluster)\n"
        "    self.__dict__.setdefault(\"_handed_out\", []).append(claim)\n"
        "    return claim\n", 1)),
    # Round 11 (A): a non-allocating C walk of every pool's credit per take().
    "credit_max_walk": (f"{P}/batch_utils.py", lambda s: s.replace(
        "        if self._is_throttled() or self._credit.get(warmpool, 0) <= 0:\n"
        "            return False\n"
        "        self._credit[warmpool] -= 1\n",
        "        if max(self._credit.values(), default=0) <= 0:\n"
        "            return False\n"
        "        if self._is_throttled() or self._credit.get(warmpool, 0) <= 0:\n"
        "            return False\n"
        "        self._credit[warmpool] -= 1\n", 1)),
    # Round 12 (A): each fill create logs its pool's member count, eagerly.
    **{f"fill_create_members{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "                self._plan_live -= 1\n                if self._warm_credit.take(group.warmpool):",
        "                self._plan_live -= 1\n"
        "                logger.debug(f\"creating {claim_name} \"\n"
        "                             f\"({len(self._state.members(group.warmpool))} in pool)\")\n"
        "                if self._warm_credit.take(group.warmpool):", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 12 (A): release_group rebuilds the whole plan when it drops anything.
    **{f"drop_rebuild_plan{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "            dropped = self._plan_by_pool.pop(warmpool, {})\n",
        "            dropped = self._plan_by_pool.pop(warmpool, {})\n"
        "            if dropped:\n"
        "                self._create_plan = deque(e for e in self._create_plan if e[0] not in dropped)\n",
        1)) for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 12 (A): release_member also wakes the newest waiting acquire.
    **{f"release_wakes_newest{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "            self._wake_acquire(member.claim_name)\n",
        "            self._wake_acquire(member.claim_name)\n"
        "            if self._acquire_waiters:\n"
        "                newest, waiter = self._acquire_waiters.popitem()\n"
        "                self._acquire_waiters[newest] = waiter\n"
        "                waiter.set()\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 12 (A): a stale connection sweeps the whole connection cache.
    **{f"stale_sweep_all{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "                        stale = self._pop_stale_connections([claim_name])\n",
        "                        stale = self._pop_stale_connections([claim_name])\n"
        "                        if stale:\n"
        "                            stale += self._pop_stale_connections(list(self._connected))\n",
        1)) for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 12 (B): every watch event prunes idle pools by walking all of them.
    **{f"credit_forget_idle{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "        for warmpool in self._state.take_exhausted_pools():\n"
        "            self._warm_credit.exhaust(warmpool)\n",
        "        for warmpool in self._state.take_exhausted_pools():\n"
        "            self._warm_credit.exhaust(warmpool)\n"
        "        for pool, read_at in list(self._warm_credit._read_at.items()):\n"
        "            if read_at < 0 and not self._warm_credit._credit.get(pool):\n"
        "                self._warm_credit._read_at.pop(pool, None)\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 12 (B): release_member wakes the oldest waiting acquire through a helper.
    **{f"release_wakes_oldest_helper{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "            self._wake_acquire(member.claim_name)\n",
        "            self._wake_acquire(member.claim_name)\n"
        "            self._wake_oldest_acquire()\n", 1).replace(
        "    def _exhaust_warm_credit(self) -> None:\n",
        "    def _wake_oldest_acquire(self) -> None:\n"
        "        for waiter in self._acquire_waiters.values():\n"
        "            waiter.set()\n"
        "            return\n\n"
        "    def _exhaust_warm_credit(self) -> None:\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 13 (B): each fill create's can_send scans every group.
    "fill_can_send_group_scan": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            def can_send() -> bool:\n                self._expire_fill_if_due()\n",
        "            def can_send() -> bool:\n                self._expire_fill_if_due()\n"
        "                if group.warmpool not in [g.warmpool for g in self._state.groups]:\n"
        "                    return False\n", 1)),
    # Round 13 (B): claim_batch counts each group's leftover claims with an extra LIST.
    "claim_count_per_group": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            if claim_items:\n                raise BatchExistsError(\n",
        "            for g in args.groups:\n"
        "                helper.count_live_sandbox_claims(\n"
        "                    namespace, batch_utils.group_label_selector(args.batch_id, g.warmpool,"
        " None))\n"
        "            if claim_items:\n                raise BatchExistsError(\n", 1)),
    # Round 13 (B): attach orders pools by an O(N * G) min over all claims.
    "attach_sort_ng": (f"{P}/batch_state.py", lambda s: s.replace(
        "    for pool in sorted(pool_order, key=lambda p: (pool_order[p], p)):\n",
        "    for pool in sorted(pool_order, key=lambda p: (min(\n"
        "            ordinal_sort_key(parse_ordinal(batch_id, (c.get('metadata') or {}).get('name', '')))\n"
        "            for c in claim_objs\n"
        "            if ((c.get('spec') or {}).get('warmPoolRef') or {}).get('name', '') == p), p)):\n",
        1)),
    # Round 13 (A): RunReport keeps every phase duration.
    "rl_report_samples": (f"{R}/observability.py", lambda s: s.replace(
        "  def add_phase(self, name: str, dur: float) -> None:\n",
        "  def add_phase(self, name: str, dur: float) -> None:\n"
        "    self.__dict__.setdefault(\"_samples\", []).append((name, dur))\n", 1)),
    # Round 13 (A): fill_settled hands the groups to a module-level helper that sums them.
    "fill_settled_helper": (f"{P}/batch_state.py", lambda s: s.replace(
        "        return self._fill_expired or len(self._resolved) + self._never_created >= self.size\n",
        "        return self._fill_expired or (\n"
        "            len(self._resolved) + self._never_created >= _fill_size(self.groups))\n", 1
    ).replace(
        "def reconstruct_groups(",
        "def _fill_size(groups):\n    import operator\n"
        "    return sum(map(operator.attrgetter(\"size\"), groups))\n\n\n"
        "def reconstruct_groups(", 1)),
    # Round 13 (A): F-39 regression, the session re-joins its whole buffer per chunk again.
    "rl_session_rejoin": (f"{R}/handles.py", lambda s: s.replace(
        "        if marker in window + chunk:\n",
        "        if marker in \"\".join(buf):\n", 1)),
    # Round 13 (B): record_pod_identity reads the pod twice.
    "rl_pod_identity_twice": (f"{R}/handles.py", lambda s: s.replace(
        "      pod = self._cluster.exec_core_api().read_namespaced_pod_status(\n"
        "          self.pod_name, self._cluster.namespace, _request_timeout=(3, 10))\n"
        "      self.pod_uid, self.runtime_restarts = _pod_identity(pod)\n",
        "      pod = self._cluster.exec_core_api().read_namespaced_pod_status(\n"
        "          self.pod_name, self._cluster.namespace, _request_timeout=(3, 10))\n"
        "      pod = self._cluster.exec_core_api().read_namespaced_pod_status(\n"
        "          self.pod_name, self._cluster.namespace, _request_timeout=(3, 10))\n"
        "      self.pod_uid, self.runtime_restarts = _pod_identity(pod)\n", 1)),
    # Round 13 (B): a batch-mode acquire GETs the pod every episode.
    "rl_batch_pod_get": (f"{R}/fleet.py", lambda s: s.replace(
        "          pod_ip = select_pod_ip(claim.member.pod_ips)\n",
        "          pod_ip = select_pod_ip(claim.member.pod_ips)\n"
        "          cluster.exec_core_api().read_namespaced_pod_status(pod, cluster.namespace)\n",
        1)),
    # Round 13 (self): is_alive reads the pod twice per step.
    "rl_is_alive_double_get": (f"{R}/handles.py", lambda s: s.replace(
        "      pod = self._cluster.exec_core_api().read_namespaced_pod_status(\n"
        "          self.pod_name, self._cluster.namespace, _request_timeout=(3, 10))\n"
        "    except client.ApiException as e:\n",
        "      self._cluster.exec_core_api().read_namespaced_pod_status(\n"
        "          self.pod_name, self._cluster.namespace, _request_timeout=(3, 10))\n"
        "      pod = self._cluster.exec_core_api().read_namespaced_pod_status(\n"
        "          self.pod_name, self._cluster.namespace, _request_timeout=(3, 10))\n"
        "    except client.ApiException as e:\n", 1)),
    # Round 13 (self): a scan of the release history per acquire.
    "acquire_scans_released": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "need_check = warmpool not in self._checked_pools",
        "need_check = warmpool not in self._checked_pools or any("
        "n == warmpool for n in self._state._released)", 1)),
    # Round 14 (A): fill_settled compares two whole sets (a C walk, no bytecode).
    "fill_settled_set_le": (f"{P}/batch_state.py", lambda s: s.replace(
        "        return self._fill_expired or len(self._resolved) + self._never_created >= self.size\n",
        "        return self._fill_expired or (\n"
        "            self._fill_claims <= self._resolved\n"
        "            and len(self._resolved) + self._never_created >= self.size)\n", 1)),
    # Round 14 (A): release_group recounts every pool's planned creates.
    **{f"drop_recount_plan{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "            self._plan_live -= len(dropped)\n",
        "            self._plan_live = sum(map(len, self._plan_by_pool.values()))\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 14 (A): a paced create counts the in-flight acquires through a local alias.
    **{f"paced_slot_alias_walk{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "        slot = max(earliest, self._next_create_at)\n",
        "        acquiring = self._state._acquiring\n"
        "        if sum(1 for _ in acquiring) < 0:\n"
        "            return earliest\n"
        "        slot = max(earliest, self._next_create_at)\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 14 (A, F4): a plan lookup scans every entry in C (no bytecode, no allocation).
    "rl_plan_values_scan": (f"{R}/fleet.py", lambda s: s.replace(
        "    return self._by_image.get(image)\n",
        "    if None in self._by_image.values():\n      return None\n"
        "    return self._by_image.get(image)\n", 1)),
    # Round 14 (B): the group outcome copies the group's waiting members on every event.
    "group_outcome_copy_each_event": (f"{P}/batch_state.py", lambda s: s.replace(
        "        if len(waiting) >= min_ready:\n            names = list(waiting)\n",
        "        names = list(waiting)\n        if len(names) >= min_ready:\n", 1)),
    # Round 14 (B): the quorum check copies the pool's waiting members on every event.
    "quorum_copy_each_event": (f"{P}/batch_state.py", lambda s: s.replace(
        "        if len(self._waiting[pool]) < self._min_ready[pool]:\n",
        "        held = list(self._waiting[pool])\n"
        "        if len(held) < self._min_ready[pool]:\n", 1)),
    # Round 14 (B): attach dedupes claims through a local list.
    "attach_seed_list_dedupe": (f"{P}/batch_state.py", lambda s: s.replace(
        "        \"\"\"Reconstructs state from the batch's existing claims.\"\"\"\n"
        "        for claim_obj in claim_objs:\n            self.upsert_claim(claim_obj)\n",
        "        \"\"\"Reconstructs state from the batch's existing claims.\"\"\"\n"
        "        seeded = []\n"
        "        for claim_obj in claim_objs:\n"
        "            name = (claim_obj.get(\"metadata\") or {}).get(\"name\", \"\")\n"
        "            if name in seeded:\n                continue\n"
        "            seeded.append(name)\n"
        "            self.upsert_claim(claim_obj)\n", 1)),
    # Round 14 (B): the 410 resync keeps the listed names in a list.
    "resync_seen_list": (f"{P}/batch_state.py", lambda s: s.replace(
        "        seen_names = set()\n", "        seen_names = []\n", 1).replace(
        "            seen_names.add(name)\n", "            seen_names.append(name)\n", 1).replace(
        "        missing = set(self._members.keys()) - seen_names\n",
        "        missing = [n for n in self._members if n not in seen_names]\n", 1)),
    # Round 15 (B): a new pool's first acquire logs every checked pool (eager f-string).
    **{f"new_pool_fstring_log{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "                    self._checked_pools.add(warmpool)\n",
        "                    self._checked_pools.add(warmpool)\n"
        "                    logger.debug(f\"checked pools: {self._checked_pools}\")\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 15 (B): checked pools rebuilt by a set union per new pool.
    "new_pool_union_rebuild": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                    self._checked_pools.add(warmpool)\n",
        "                    self._checked_pools = self._checked_pools | {warmpool}\n", 1)),
    # Round 15 (B): warm credit rebuilt by dict unpacking per record.
    "credit_record_unpack": (f"{P}/batch_utils.py", lambda s: s.replace(
        "        self._credit[warmpool] = ready\n",
        "        self._credit = {**self._credit, warmpool: ready}\n", 1)),
    # Round 15 (B): a metric label per task id (unbounded cardinality).
    "rl_family_label_task_id": (f"{R}/fleet.py", lambda s: s.replace(
        "    fam = repo_family(task)\n", "    fam = task.id\n", 1)),
    # Round 15 (A): terminate untracks a handle by scanning the client's registry.
    **{f"untrack_scan{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "        if registry.get(key) is sandbox:\n            del registry[key]\n",
        "        for tracked_key, tracked in list(registry.items()):\n"
        "            if tracked is sandbox:\n                del registry[tracked_key]\n", 1))
       for sfx, mod in (("", "sandbox_client.py"), ("_async", "async_sandbox_client.py"))},
    # Round 15 (A): forget_claim_in_client scans the client's registry for the claim.
    "rl_forget_scan": (f"{R}/handles.py", lambda s: s.replace(
        "    active.pop(key, None)\n",
        "    for k in [k for k in active if k[1] == claim_name]:\n      active.pop(k, None)\n", 1)),
    # Round 15 (A): every release retries the whole stale-connection backlog.
    **{f"stale_retry_each_release{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "        if claim_names is None:\n            with self._lock:\n"
        "                stale = list(self._stale_connections)\n",
        "        if True:\n            with self._lock:\n"
        "                stale = list(self._stale_connections)\n", 1).replace(
        "        if claim_names is None:\n            async with self._lock:\n"
        "                stale = list(self._stale_connections)\n",
        "        if True:\n            async with self._lock:\n"
        "                stale = list(self._stale_connections)\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 15 (A): a failed acquire wakes every waiting acquire.
    **{f"drop_notify_all{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "            needs_delete = self._state.drop_acquired(claim_name, may_exist, retried=retried)\n"
        "            self._changed.notify_all()\n",
        "            needs_delete = self._state.drop_acquired(claim_name, may_exist, retried=retried)\n"
        "            self._notify_all()\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 16 (A): the fill plan becomes a list drained from its head.
    **{f"plan_list_pop0{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "        self._create_plan: deque[tuple[str, BatchGroup]] = deque()\n",
        "        self._create_plan: list[tuple[str, BatchGroup]] = []\n", 1).replace(
        "        handle._create_plan = deque(batch_utils.fill_create_plan(args.batch_id, args.groups))\n",
        "        handle._create_plan = list(batch_utils.fill_create_plan(args.batch_id, args.groups))\n",
        1).replace(
        "                claim_name, group = self._create_plan.popleft()\n",
        "                claim_name, group = self._create_plan.pop(0)\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))},
    # Round 16 (A): create_sandbox finds the existing handle by scanning the registry.
    "client_registry_scan": (f"{P}/sandbox_client.py", lambda s: s.replace(
        "            existing = self._active_connection_sandboxes.get((namespace, claim_name))\n",
        "            existing = next((t for k, t in self._active_connection_sandboxes.items()\n"
        "                             if k == (namespace, claim_name)), None)\n", 1)),
    # Round 16 (A): create_sandbox reads the Sandbox once more.
    "client_extra_get": (f"{P}/sandbox_client.py", lambda s: s.replace(
        "            existing = self._active_connection_sandboxes.get((namespace, claim_name))\n",
        "            self.k8s_helper.get_sandbox(sandbox_id, namespace)\n"
        "            existing = self._active_connection_sandboxes.get((namespace, claim_name))\n", 1)),
    # Round 16 (A): the renew loop polls in half-second slices.
    "renew_poll": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            if self._renew_stop.wait(max(0.0, min(wait, deadline - time.monotonic()))):\n",
        "            if self._renew_stop.wait(min(0.5, max(0.0, min(wait, deadline - time.monotonic())))):\n",
        1)),
    "renew_poll_async": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "            await asyncio.sleep(max(0.0, min(wait, deadline - time.monotonic())))\n",
        "            await asyncio.sleep(min(0.5, max(0.0, min(wait, deadline - time.monotonic()))))\n",
        1)),
    # Round 16 (A): the session polls its websocket every 10 ms.
    "rl_session_poll": (f"{R}/handles.py", lambda s: s.replace(
        "        upd = 1.0\n", "        upd = 0.01\n", 1)),
    # Round 16 (A): the claim watchdog re-resolves the pod on every poll.
    "rl_watchdog_reresolve": (f"{R}/failfast.py", lambda s: s.replace(
        "    if self.pod_name is None:\n      self.pod_name = self._resolve_pod_name()\n",
        "    if True:\n      self.pod_name = self._resolve_pod_name()\n", 1)),
    # Round 16 (B): create_sandbox prunes every tracked handle first.
    "client_create_prunes_all": (f"{P}/sandbox_client.py", lambda s: s.replace(
        "        previous = self._active_connection_sandboxes.get((namespace, claim_name))\n",
        "        self.list_active_sandboxes()\n"
        "        previous = self._active_connection_sandboxes.get((namespace, claim_name))\n", 1)),
    # Round 16 (B): the Lease renews ten times as often.
    "renew_interval_tenth": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "        self._renew_interval = max(1, lease_duration // 3)\n",
        "        self._renew_interval = max(1, lease_duration // 30)\n", 1)),
    # Round 16 (B): claim lists page 5 at a time (100x the LIST requests).
    "list_page_size_5": (f"{P}/constants.py", lambda s: s.replace(
        "CLAIM_LIST_PAGE_SIZE = 500\n", "CLAIM_LIST_PAGE_SIZE = 5\n", 1)),
    # Round 11: a per_claim release no longer forgets the claim in the SDK client.
    "rl_per_claim_no_forget": (f"{R}/handles.py", lambda s: s.replace(
        "    if self._cluster is not None:\n      forget_claim_in_client(self._cluster, self.claim_name)\n",
        "", 1)),
    # Round 17 (A): the stderr branch re-joins the whole buffer per chunk.
    "rl_session_stderr_rejoin": (f"{R}/handles.py", lambda s: s.replace(
        "        buf.append(chunk)\n        window = (window + chunk)[-(len(marker) - 1):]\n",
        "        buf.append(chunk)\n        window = \"\".join(buf[:])[-(len(marker) - 1):]\n", 1)),
    # Round 17 (A, B): the background loops poll at most every quarter second.
    "rl_watchdog_fast_poll": (f"{R}/failfast.py", lambda s: s.replace(
        "      if self._stop_evt.wait(self.policy.poll_s):\n",
        "      if self._stop_evt.wait(min(self.policy.poll_s, 0.25)):\n", 1)),
    "rl_breaker_fast_poll": (f"{R}/fleet.py", lambda s: s.replace(
        "      while not stop.wait(self.config.breaker_poll_s):\n",
        "      while not stop.wait(min(self.config.breaker_poll_s, 0.25)):\n", 1)),
    # Round 17 (A): add_tasks re-sums every planned image's tasks.
    "rl_add_tasks_resum": (f"{R}/fleet.py", lambda s: s.replace(
        "        cluster_totals: dict[str, int] = collections.defaultdict(int, plan.cluster_tasks)\n",
        "        cluster_totals: dict[str, int] = collections.defaultdict(int)\n"
        "        for e in plan.entries:\n"
        "          cluster_totals[e.cluster] += e.tasks\n", 1)),
    # Round 17 (A, B): a caller-named create scans or prunes _explicit_claims.
    **{f"client_explicit_list_scan{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "            self._explicit_claims.add((namespace, claim_name))\n",
        "            if (namespace, claim_name) not in list(self._explicit_claims):\n"
        "                self._explicit_claims.add((namespace, claim_name))\n", 1))
       for sfx, mod in (("", "sandbox_client.py"), ("_async", "async_sandbox_client.py"))},
    **{f"client_explicit_intersect{sfx}": (f"{P}/{mod}", lambda s: s.replace(
        "            self._explicit_claims.add((namespace, claim_name))\n",
        "            self._explicit_claims.intersection_update(self._active_connection_sandboxes)\n"
        "            self._explicit_claims.add((namespace, claim_name))\n", 1))
       for sfx, mod in (("", "sandbox_client.py"), ("_async", "async_sandbox_client.py"))},
    # Round 17 (B): each per-claim cleanup retries one stuck stale close.
    "stale_retry_one": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "        if claim_names is None:\n            with self._lock:\n"
        "                stale = list(self._stale_connections)\n",
        "        if claim_names is not None and self._stale_connections:\n"
        "            retry, _ = self._stale_connections.popitem()\n"
        "            self._stale_connections[retry] = None\n"
        "            try:\n                retry.close_connection()\n"
        "            except Exception:\n                pass\n"
        "        if claim_names is None:\n            with self._lock:\n"
        "                stale = list(self._stale_connections)\n", 1)),
    "stale_retry_one_async": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "        if claim_names is None:\n            async with self._lock:\n"
        "                stale = list(self._stale_connections)\n",
        "        if claim_names is not None and self._stale_connections:\n"
        "            retry, _ = self._stale_connections.popitem()\n"
        "            self._stale_connections[retry] = None\n"
        "            try:\n                await retry.close_connection()\n"
        "            except Exception:\n                pass\n"
        "        if claim_names is None:\n            async with self._lock:\n"
        "                stale = list(self._stale_connections)\n", 1)),
    # Round 18 (A, B): process_parallel finds each finished future by scanning a list.
    "rl_executor_index": (f"{R}/strategies.py", lambda s: s.replace(
        "    futs = {ex.submit(_one, t): i for i, t in enumerate(tasks)}\n"
        "    for fut in as_completed(futs):\n      i = futs[fut]\n",
        "    futs = [ex.submit(_one, t) for t in tasks]\n"
        "    for fut in as_completed(futs):\n      i = futs.index(fut)\n", 1)),
    # Round 18 (A, B): the run report keeps a record per task.
    "rl_report_task_log": (f"{R}/observability.py", lambda s: s.replace(
        "      self.tasks_ok += 1\n",
        "      self.tasks_ok += 1\n      self.__dict__.setdefault(\"_task_log\", []).append(status)\n",
        1)),
    # Round 18 (A): idle waits capped at 5 s instead of the deadline.
    "next_wait_cap": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                self._changed.wait(timeout)\n",
        "                self._changed.wait(min(timeout, 5.0))\n", 1)),
    "next_wait_cap_async": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "                    await asyncio.wait_for(self._changed.wait(), timeout)\n",
        "                    await asyncio.wait_for(self._changed.wait(), min(timeout, 5.0))\n", 1)),
    # Round 18 (A, B): an INFO record per watch event or per acquire check.
    "info_log_watch": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                    event_type = event.get(\"type\")\n"
        "                    obj = event.get(\"object\") or {}\n",
        "                    event_type = event.get(\"type\")\n"
        "                    obj = event.get(\"object\") or {}\n"
        "                    logger.info(f\"watch {event_type} {obj.get('status')}\")\n", 1)),
    "info_log_acquire": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                member = self._state.acquired_member(claim_name)\n",
        "                member = self._state.acquired_member(claim_name)\n"
        "                logger.info(f\"acquire check {claim_name}\")\n", 1)),
    # Round 18 (B): a claim request sent while holding the batch lock.
    "delete_under_lock": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "                        seq = self._state.delete_seq(claim_name)\n"
        "                    self._client.k8s_helper.delete_sandbox_claim(\n",
        "                        seq = self._state.delete_seq(claim_name)\n"
        "                        self._client.k8s_helper.delete_sandbox_claim(\n", 1)),
    "create_under_lock_async": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "            await self._client.k8s_helper.create_sandbox_claim(\n",
        "            async with self._lock:\n"
        "                await self._client.k8s_helper.create_sandbox_claim(\n", 1)),
    # Round 19 (pre-launch inventory): the async fleet's executor finds each task's slot by
    # scanning the tasks.
    "rl_async_executor_index": (f"{R}/async_fleet.py", lambda s: s.replace(
        "              results[i] = await self._call(process_fn, task, handle)\n",
        "              results[tasks.index(task)] = await self._call(process_fn, task, handle)\n",
        1)),
    # The recycle executor counts the unprocessed tasks after every task.
    "rl_recycle_results_scan": (f"{R}/recycle.py", lambda s: s.replace(
        "        results[i] = process_fn(t, handle)\n",
        "        results[i] = process_fn(t, handle)\n        _pending = results.count(_UNSET)\n", 1)),
    # The recycle executor claims a fresh sandbox for every task.
    "rl_recycle_claim_per_task": (f"{R}/recycle.py", lambda s: s.replace(
        "        if not recyclable:                    # can't reset",
        "        if True:                              # can't reset", 1)),
    # The lookahead warmer rescales every active pool on each reconcile.
    "rl_lookahead_rescale_all": (f"{R}/lookahead.py", lambda s: s.replace(
        "                if img in active and active[img] != n]\n",
        "                if img in active]\n", 1)),
    # The lookahead warmer keeps every finished batch.
    "rl_lookahead_keep_history": (f"{R}/lookahead.py", lambda s: s.replace(
        "      if self.keep_finished:\n        self._finished.append(counts)\n",
        "      self._finished.append(counts)\n", 1)),
    # The per_claim create runs under the fleet's lock.
    "rl_create_under_fleet_lock": (f"{R}/fleet.py", lambda s: s.replace(
        "          sandbox = cluster.sandbox_client.create_sandbox(\n",
        "          with self._lock:\n            sandbox = cluster.sandbox_client.create_sandbox(\n",
        1)),
    # Suspend scans the live handles under the fleet's lock.
    "rl_suspend_scan": (f"{R}/fleet.py", lambda s: s.replace(
        "      if handle._slot_released:\n        return\n      handle._slot_released = True\n",
        "      if handle._slot_released:\n        return\n"
        "      _tracked = handle in list(self._handles.values())\n"
        "      handle._slot_released = True\n", 1)),
    # The async fleet's executor keeps a record per task.
    "rl_async_executor_keeps_tasks": (f"{R}/async_fleet.py", lambda s: s.replace(
        "              results[i] = await self._call(process_fn, task, handle)\n",
        "              results[i] = await self._call(process_fn, task, handle)\n"
        "              self.__dict__.setdefault(\"_done\", []).append(task.id)\n", 1)),
    # delete_sandbox finds the tracked sandbox by scanning the registry.
    **{f"client_delete_scan{sfx}": (f"{P}/{mod}", lambda s, ind=ind: s.replace(
        f"{ind}sandbox = self._active_connection_sandboxes.get(key)\n",
        f"{ind}sandbox = next((v for k, v in self._active_connection_sandboxes.items()"
        f" if k == key), None)\n", 1))
       for sfx, mod, ind in (("", "sandbox_client.py", " " * 8),
                             ("_async", "async_sandbox_client.py", " " * 12))},
    # A paced acquire or fill create polls until its slot instead of sleeping once.
    "pacing_poll_acquire": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "            if not self._stop_creating.wait(max(0.0, slot - time.monotonic())):\n",
        "            while time.monotonic() < slot and not self._stop_creating.wait(\n"
        "                    min(slot - time.monotonic(), 0.05)):\n                pass\n"
        "            if not self._stop_creating.is_set():\n", 1)),
    "pacing_poll_fill_async": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "            if await self._wait_for_stop(slot - time.monotonic()):\n                return\n",
        "            while time.monotonic() < slot:\n"
        "                if await self._wait_for_stop(min(slot - time.monotonic(), 0.05)):\n"
        "                    return\n", 1)),
    # Round 19 (B): release_member defers a sweep of every cached connection to the event loop
    # (async) or to a thread (sync), outside the call itself.
    "deferred_sweep_async": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "        await self._close_cached_connections(\"release_member\", [member.claim_name])\n",
        "        await self._close_cached_connections(\"release_member\", [member.claim_name])\n"
        "        asyncio.get_running_loop().create_task(_sweep_stale(self))\n", 1)
        + "\n\nasync def _sweep_stale(batch):\n    async with batch._lock:\n"
          "        batch._pop_stale_connections(list(batch._connected))\n"),
    "deferred_sweep_thread": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "        self._close_cached_connections(\"release_member\", [member.claim_name])\n",
        "        self._close_cached_connections(\"release_member\", [member.claim_name])\n"
        "        threading.Thread(target=self._sweep_stale, daemon=True).start()\n", 1).replace(
        "    def release_member(self, member: Member) -> None:\n",
        "    def _sweep_stale(self) -> None:\n        with self._lock:\n"
        "            self._pop_stale_connections(list(self._connected))\n\n"
        "    def release_member(self, member: Member) -> None:\n", 1)),
    # Round 19 (A): a failed fill create wakes every waiting acquire.
    **{f"failed_fill_wakes_all{sfx}": (f"{P}/{mod}", lambda s, lock=lock: s.replace(
        f"                {lock}:\n                    self._state.record_create_failure(\n"
        "                        claim_name, group.warmpool, _create_failure_message(e)\n"
        "                    )\n                    self._changed.notify_all()\n",
        f"                {lock}:\n                    self._state.record_create_failure(\n"
        "                        claim_name, group.warmpool, _create_failure_message(e)\n"
        "                    )\n                    self._notify_all()\n", 1))
       for sfx, mod, lock in (("", "sandbox_batch.py", "with self._lock"),
                              ("_async", "async_sandbox_batch.py", "async with self._lock"))},
    # Round 19 (A): recording a failed create scans the members (inside a lambda).
    "failed_create_scan": (f"{P}/batch_state.py", lambda s: s.replace(
        "        if claim_name in self._members or claim_name in self._released:\n",
        "        observed = lambda: any(m.claim_name == claim_name for m in self._members.values())\n"
        "        if observed() or claim_name in self._released:\n", 1)),
    # Round 19 (A): a failed release wakes every waiting acquire.
    "failed_release_wakes_all": (f"{P}/sandbox_batch.py", lambda s: s.replace(
        "        if needs_delete:\n            self._delete_claim(member.claim_name)\n",
        "        if needs_delete:\n            try:\n                self._delete_claim(member.claim_name)\n"
        "            except Exception:\n                with self._lock:\n"
        "                    self._notify_all()\n                raise\n", 1)),
    # Round 19 (A): the watch wrapper buffers the whole stream before yielding.
    "watch_buffers_stream": ("clients/python/agentic-sandbox-client/k8s_agent_sandbox/k8s_helper.py",
                             lambda s: s.replace(
        "                if event is not None:\n                    yield event\n        finally:\n"
        "            w.stop()\n\n    def read_batch_lease(",
        "                if event is not None:\n                    buffered.append(event)\n"
        "            yield from buffered\n        finally:\n"
        "            w.stop()\n\n    def read_batch_lease(", 1).replace(
        "        w = watch.Watch()\n        try:\n            for event in w.stream(\n"
        "                func=self._batch_custom_objects_api.list_namespaced_custom_object,\n",
        "        w = watch.Watch()\n        buffered = []\n        try:\n            for event in w.stream(\n"
        "                func=self._batch_custom_objects_api.list_namespaced_custom_object,\n", 1)),
    # Round 19 (pre-launch): enter_acquire totals the in-flight checks of every pool (a C walk
    # of a Counter table, which only the subclass-aware walk counter sees).
    "counter_table_walk": (f"{P}/batch_state.py", lambda s: s.replace(
        "        self._checking_acquires[warmpool] += 1\n",
        "        self._checking_acquires[warmpool] += 1\n"
        "        _in_flight = sum(self._checking_acquires.values())\n", 1)),
    # Round 19 (pre-launch): a refused release scans the live handles before restoring one.
    "rl_refused_release_scan": (f"{R}/fleet.py", lambda s: s.replace(
        "      with self._lock:\n        self._handles[id(handle)] = handle\n      raise\n",
        "      with self._lock:\n        _seen = handle in list(self._handles.values())\n"
        "        self._handles[id(handle)] = handle\n      raise\n", 1)),
    # Round 20 (B): per-item connection cleanup sweeps every cached connection every K-th call.
    **{f"periodic_sweep{sfx}_{period}": (f"{P}/{mod}", lambda s, period=period: s.replace(
        "            names = list(self._connected) if claim_names is None else claim_names\n",
        "            names = list(self._connected) if claim_names is None else claim_names\n"
        "            if claim_names is not None:\n"
        "                self.__dict__[\"_close_calls\"] = self.__dict__.get(\"_close_calls\", 0) + 1\n"
        f"                if self._close_calls % {period} == 0:\n"
        "                    names = [*names, *(n for n in self._connected\n"
        "                                       if self._state.get_member(n) is None)]\n", 1))
       for sfx, mod in (("", "sandbox_batch.py"), ("_async", "async_sandbox_batch.py"))
       for period in (16, 1000)},
    # Round 20 (A): the real pool ops find their plan entry by scanning the plan.
    "rl_pool_ops_scan": (f"{R}/fleet.py", lambda s: s.replace(
        "(self.plan_ or self.plan()).for_image(image)",
        "next((e for e in (self.plan_ or self.plan()).entries if e.image == image), None)")),
    # Round 20 (A): the lookahead warmer remembers every batch it has seen.
    "rl_lookahead_seen": (f"{R}/lookahead.py", lambda s: s.replace(
        "    with self._lock:\n      self._live[batch_id] = clean\n",
        "    with self._lock:\n      seen = self.__dict__.setdefault(\"_seen_batches\", [])\n"
        "      if batch_id not in seen:\n        seen.append(batch_id)\n"
        "      self._live[batch_id] = clean\n", 1)),
    # Round 20 (B): an async release defers a scan of every connection to a timer callback.
    "timer_deferral_async": (f"{P}/async_sandbox_batch.py", lambda s: s.replace(
        "        await self._close_cached_connections(\"release_member\", [member.claim_name])\n",
        "        await self._close_cached_connections(\"release_member\", [member.claim_name])\n"
        "        asyncio.get_running_loop().call_later(0, _scan_all, self)\n", 1)
        + "\n\ndef _scan_all(batch):\n    return [n for n in batch._connected if batch._state.get_member(n)]\n"),
    # Round 20 (B): a release checks every pending delete.
    "pending_delete_walk": (f"{P}/batch_state.py", lambda s: s.replace(
        "        member = self._members.get(claim_name)\n",
        "        _outstanding = any(self._pending_deletes.values())\n"
        "        member = self._members.get(claim_name)\n", 1)),
    # Round 20 (pre-launch): each git-restore reset sends a second exec.
    "rl_reset_extra_exec": (f"{R}/recycle.py", lambda s: s.replace(
        "      d = self._parse(handle.exec([\"bash\", \"-lc\", script],\n",
        "      handle.exec([\"true\"], **_runtime_exec_kwargs(handle))\n"
        "      d = self._parse(handle.exec([\"bash\", \"-lc\", script],\n", 2)),
    # Round 20 (B): a release hands every connection to a batch_utils helper.
    "cross_module_helper": [
        (f"{P}/sandbox_batch.py", lambda s: s.replace(
            "        self._close_cached_connections(\"release_member\", [member.claim_name])\n",
            "        self._close_cached_connections(\"release_member\", [member.claim_name])\n"
            "        batch_utils.count_entries(self._connected)\n", 1)),
        (f"{P}/batch_utils.py", lambda s: s
            + "\n\ndef count_entries(table):\n    return sum(1 for _ in table)\n"),
    ],
}

WANTED = set(sys.argv[3:])
unknown = WANTED - set(MUTANTS)
assert not unknown, f"unknown mutants: {sorted(unknown)}"
for name, edits in MUTANTS.items():
    if WANTED and name not in WANTED:
        continue
    dest = OUT / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest, ignore=shutil.ignore_patterns("__pycache__", ".mypy_cache"))
    # A mutant is one (file, edit) pair, or a list of them for a change spanning files.
    for rel, mutate in edits if isinstance(edits, list) else [edits]:
        target = dest / rel
        before = target.read_text()
        after = mutate(before)
        assert after != before, (name, rel)
        target.write_text(after)
    print("created", name)
