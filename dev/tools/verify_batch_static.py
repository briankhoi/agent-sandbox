#!/usr/bin/env python3
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
"""Static AST and call-graph auditor for the agent-sandbox batch stack and RL integration.

Executes seven deterministic structural checks across the Python SDK batch modules and
examples/agent-sandbox-rl (skipping optional downstream RL files when absent on SDK-only branches):
  1. SHARED_AUTHORITY_ISOLATION: Flags watch-stream error handlers that write into the
     shared error channel read by Lease renewal, Lease-expiry notification, and release gates.
  2. ASYNC_AWAIT_UNDER_LOCK: Flags direct or transitive ``await`` / ``async for`` / ``async with``
     inside state-mutex locks (excluding ``Condition.wait`` and documented single-flight I/O locks).
  3. LOCK_HIERARCHY_AND_GUARD: Verifies transitive sync lock ordering
     (``_lifecycle_lock -> client._batches_lock -> (_lock | _changed)`` and
     ``_cluster_lock -> _lock``), verifies ``_wait_active_deletes`` never runs inside
     ``_lifecycle_lock``, and flags unlocked reads of lock-guarded handle attributes.
  4. K8S_TIMEOUT_AND_CLIENT: Verifies outbound Kubernetes calls on batch and Lease-fenced
     paths pass explicit non-None request timeouts, forward ``_request_timeout`` in helpers,
     and route batch CRD operations through ``_batch_custom_objects_api`` (`batch=True`).
  5. BASE_EXCEPTION_ROLLBACK: Flags reservation, executor-thread, and live-handle cleanup
     paths that catch ``Exception`` instead of ``BaseException`` / ``finally``, orphan
     executor handles on ``CancelledError``, or clear ``_opened`` before ``release()``.
  6. SYNC_ASYNC_AST_PARITY: Verifies method surface parity between ``SandboxBatch`` and
     ``AsyncSandboxBatch`` and checks ``CANCEL-5`` statement ordering in async cleanup paths.
  7. PEP561_PUBLIC_EXPORTS: Verifies ``k8s_agent_sandbox/__init__.py`` declares explicit
     ``__all__`` re-exports for every public symbol when ``py.typed`` is shipped.
"""

from __future__ import annotations

import argparse
import ast
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Iterator


@dataclasses.dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    invariant: str
    rel_path: str
    line: int
    symbol: str
    message: str


SDK_REL = "clients/python/agentic-sandbox-client/k8s_agent_sandbox"
RL_REL = "examples/agent-sandbox-rl/agent_sandbox_rl"


def _parse_if_exists(repo_root: pathlib.Path, rel_path: str) -> tuple[ast.Module, list[str]] | None:
    full = repo_root / rel_path
    if not full.exists():
        return None
    source = full.read_text(encoding="utf-8")
    return ast.parse(source, filename=rel_path), source.splitlines()


def _iter_no_nested_defs(root: ast.AST) -> Iterator[ast.AST]:
    """Yields AST descendants of ``root`` without descending into nested functions or lambdas."""
    stack: list[ast.AST] = list(reversed(list(ast.iter_child_nodes(root))))
    while stack:
        cur = stack.pop()
        yield cur
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        children = list(ast.iter_child_nodes(cur))
        stack.extend(reversed(children))


def _attr_chain(node: ast.AST) -> str | None:
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(cur, ast.Call):
        base = _attr_chain(cur.func)
        if base is not None:
            return f"{base}()"
    return None


def _lock_name_from_withitem(item: ast.withitem) -> str | None:
    return _attr_chain(item.context_expr)


def _is_condition_wait_await(await_node: ast.Await) -> bool:
    """Returns True when an ``await`` is ``self._changed.wait()`` (including wrapped in
    ``asyncio.wait_for``).
    """
    val = await_node.value
    if not isinstance(val, ast.Call):
        return False
    target = _attr_chain(val.func)
    if target in {"self._changed.wait", "self._changed.wait_for"}:
        return True
    if target == "asyncio.wait_for" and val.args and isinstance(val.args[0], ast.Call):
        inner_target = _attr_chain(val.args[0].func)
        if inner_target in {"self._changed.wait", "self._changed.wait_for"}:
            return True
    return False


def _class_methods(
    tree: ast.Module,
) -> dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    out: dict[str, dict[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name] = item
            out[node.name] = methods
    return out


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


# ---------------------------------------------------------------------------
# Check 1: Shared error channel authority separation (Watch vs Lease renewal)
# ---------------------------------------------------------------------------


def check_shared_authority_isolation(repo_root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel_path, cls_name in [
        (f"{SDK_REL}/sandbox_batch.py", "SandboxBatch"),
        (f"{SDK_REL}/async_sandbox_batch.py", "AsyncSandboxBatch"),
    ]:
        parsed = _parse_if_exists(repo_root, rel_path)
        if parsed is None:
            continue
        tree, _ = parsed
        methods = _class_methods(tree).get(cls_name, {})
        watch_loop = methods.get("_watch_loop")
        if watch_loop is None:
            continue

        watch_notes_state_error: list[int] = [
            sub.lineno
            for sub in _iter_no_nested_defs(watch_loop)
            if isinstance(sub, ast.Call) and _attr_chain(sub.func) == "self._state.note_error"
        ]
        if not watch_notes_state_error:
            continue

        coupled_readers: list[str] = []
        for reader_name in ("_renew_once", "_expire_lease_if_due", "_check_healthy"):
            reader_node = methods.get(reader_name)
            if reader_node is None:
                continue
            lines = [
                sub.lineno
                for sub in _iter_no_nested_defs(reader_node)
                if isinstance(sub, ast.Call) and _attr_chain(sub.func) == "self._state.error"
            ]
            if lines:
                coupled_readers.append(f"{reader_name}:{lines}")

        if coupled_readers:
            findings.append(
                Finding(
                    check="SHARED_AUTHORITY_ISOLATION",
                    severity="CRITICAL",
                    invariant="INV-AUTH-1",
                    rel_path=rel_path,
                    line=watch_notes_state_error[0],
                    symbol=f"{cls_name}._watch_loop",
                    message=(
                        f"`_watch_loop` writes fatal watch errors to shared "
                        f"`self._state.note_error(...)` "
                        f"(lines {watch_notes_state_error}), which couples watch failure into "
                        f"{', '.join(coupled_readers)} (`INV-AUTH-1`/`INV-AUTH-2`/`INV-AUTH-3`): "
                        f"halting Lease "
                        f"renewal, suppressing Lease-expiry notifications, and blocking "
                        f"`release_member` / "
                        f"`release_group`."
                    ),
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Check 2: Direct or transitive await under asyncio.Lock / Condition
# ---------------------------------------------------------------------------


def check_async_await_under_lock(repo_root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []
    targets = [
        f"{SDK_REL}/async_sandbox_batch.py",
        f"{SDK_REL}/async_sandbox_client.py",
        f"{SDK_REL}/async_k8s_helper.py",
    ]
    # State mutexes that must never be held across network/subprocess suspension points.
    # Intentional single-flight I/O locks (`self._lifecycle_lock`, `self._init_lock` in
    # AsyncK8sHelper._ensure_initialized, and per-warmpool `refresh_lock` in
    # AsyncSandboxBatch._refresh_warm_credit) serialize I/O by design and are excluded here.
    tracked_locks = {"self._lock", "self._changed", "batch._lock", "batch._changed"}

    client_methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    client_parsed = _parse_if_exists(repo_root, f"{SDK_REL}/async_sandbox_client.py")
    if client_parsed is not None:
        client_methods = _class_methods(client_parsed[0]).get("AsyncSandboxClient", {})

    for rel_path in targets:
        parsed = _parse_if_exists(repo_root, rel_path)
        if parsed is None:
            continue
        tree, _ = parsed
        mod_funcs = _module_functions(tree)
        classes = _class_methods(tree)

        for cls_name, methods in classes.items():
            direct_yields: dict[str, list[tuple[int, str]]] = {}
            call_edges: dict[str, list[tuple[int, str]]] = {}

            all_callables: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
            for fn_name, fn_node in mod_funcs.items():
                all_callables[f"mod:{fn_name}"] = fn_node
            for mname, mnode in methods.items():
                all_callables[f"self:{mname}"] = mnode
            for cm_name, cm_node in client_methods.items():
                all_callables[f"client:{cm_name}"] = cm_node

            for key, cnode in all_callables.items():
                dy: list[tuple[int, str]] = []
                edges: list[tuple[int, str]] = []
                for sub in _iter_no_nested_defs(cnode):
                    if isinstance(sub, ast.Await):
                        if _is_condition_wait_await(sub):
                            continue
                        dy.append((sub.lineno, ast.unparse(sub)))
                    elif isinstance(sub, ast.AsyncFor):
                        dy.append((sub.lineno, "async for"))
                    elif isinstance(sub, ast.AsyncWith):
                        dy.append((
                            sub.lineno,
                            f"async with {ast.unparse(sub.items[0].context_expr)}",
                        ))
                    elif isinstance(sub, ast.Call):
                        chain = _attr_chain(sub.func)
                        if not chain:
                            continue
                        if chain.startswith("self.") and chain.count(".") == 1:
                            callee = f"self:{chain.split('.', 1)[1]}"
                            if callee in all_callables:
                                edges.append((sub.lineno, callee))
                        elif chain.startswith("self._client.") and chain.count(".") == 2:
                            callee = f"client:{chain.split('.')[2]}"
                            if callee in all_callables:
                                edges.append((sub.lineno, callee))
                        elif "." not in chain and f"mod:{chain}" in all_callables:
                            edges.append((sub.lineno, f"mod:{chain}"))
                direct_yields[key] = dy
                call_edges[key] = edges

            def reachable_yield(start_key: str) -> tuple[str, int, str] | None:
                visited: set[str] = set()
                stack = [start_key]
                while stack:
                    cur = stack.pop()
                    if cur in visited:
                        continue
                    visited.add(cur)
                    cnode = all_callables[cur]
                    if isinstance(cnode, ast.AsyncFunctionDef) and direct_yields.get(cur):
                        line, expr = direct_yields[cur][0]
                        return cur, line, expr
                    for _, nxt in call_edges.get(cur, []):
                        if nxt not in visited:
                            stack.append(nxt)
                return None

            for mname, mnode in methods.items():
                for sub in _iter_no_nested_defs(mnode):
                    if not isinstance(sub, ast.AsyncWith):
                        continue
                    held = [
                        lk
                        for item in sub.items
                        if (lk := _lock_name_from_withitem(item)) in tracked_locks
                    ]
                    if not held:
                        continue
                    lock_label = ", ".join(held)
                    for body_stmt in sub.body:
                        for inner in [body_stmt] + list(_iter_no_nested_defs(body_stmt)):
                            if isinstance(inner, ast.Await):
                                if _is_condition_wait_await(inner):
                                    continue
                                findings.append(
                                    Finding(
                                        check="ASYNC_AWAIT_UNDER_LOCK",
                                        severity="MEDIUM",
                                        invariant="INV-LOCK-ASYNC-1",
                                        rel_path=rel_path,
                                        line=inner.lineno,
                                        symbol=f"{cls_name}.{mname}",
                                        message=(
                                            f"Direct await `{ast.unparse(inner)}` executes while "
                                            f"holding "
                                            f"`{lock_label}`, blocking other coroutines waiting on "
                                            f"`{lock_label}` "
                                            f"across network or subprocess I/O."
                                        ),
                                    )
                                )
                            elif isinstance(inner, ast.AsyncFor):
                                findings.append(
                                    Finding(
                                        check="ASYNC_AWAIT_UNDER_LOCK",
                                        severity="HIGH",
                                        invariant="INV-LOCK-ASYNC-1",
                                        rel_path=rel_path,
                                        line=inner.lineno,
                                        symbol=f"{cls_name}.{mname}",
                                        message=(
                                            f"Direct `async for` executes while holding "
                                            f"`{lock_label}`, "
                                            f"yielding across asynchronous stream iteration."
                                        ),
                                    )
                                )
                            elif isinstance(inner, ast.AsyncWith):
                                ctx_txt = ", ".join(
                                    ast.unparse(it.context_expr) for it in inner.items
                                )
                                findings.append(
                                    Finding(
                                        check="ASYNC_AWAIT_UNDER_LOCK",
                                        severity="HIGH",
                                        invariant="INV-LOCK-ASYNC-1",
                                        rel_path=rel_path,
                                        line=inner.lineno,
                                        symbol=f"{cls_name}.{mname}",
                                        message=(
                                            f"Nested `async with {ctx_txt}` executes while holding "
                                            f"`{lock_label}`."
                                        ),
                                    )
                                )
                            elif isinstance(inner, ast.Call):
                                chain = _attr_chain(inner.func)
                                target_key: str | None = None
                                if chain and chain.startswith("self.") and chain.count(".") == 1:
                                    cand = f"self:{chain.split('.', 1)[1]}"
                                    if cand in all_callables:
                                        target_key = cand
                                elif (
                                    chain
                                    and chain.startswith("self._client.")
                                    and chain.count(".") == 2
                                ):
                                    cand = f"client:{chain.split('.')[2]}"
                                    if cand in all_callables:
                                        target_key = cand
                                elif chain and "." not in chain and f"mod:{chain}" in all_callables:
                                    target_key = f"mod:{chain}"
                                if target_key is not None:
                                    hit = reachable_yield(target_key)
                                    if hit is not None:
                                        ymeth, yline, yexpr = hit
                                        findings.append(
                                            Finding(
                                                check="ASYNC_AWAIT_UNDER_LOCK",
                                                severity="HIGH",
                                                invariant="INV-LOCK-ASYNC-1",
                                                rel_path=rel_path,
                                                line=inner.lineno,
                                                symbol=f"{cls_name}.{mname}",
                                                message=(
                                                    f"Call `{chain}()` under `{lock_label}` "
                                                    f"transitively "
                                                    f"reaches suspension `{yexpr}` in `{ymeth}` "
                                                    f"(line {yline})."
                                                ),
                                            )
                                        )
    return findings


# ---------------------------------------------------------------------------
# Check 3: Sync lock hierarchy, wait-under-lifecycle-lock, & lock-guarded reads
# ---------------------------------------------------------------------------


def check_lock_hierarchy_and_guards(repo_root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []

    def audit_sync_lock_graph(
        callables: dict[str, tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
        rank_map: dict[str, int],
        resolve_call: dict[tuple[str, str], str],
        check_wait_active_deletes: bool = False,
    ) -> None:
        direct_locks: dict[str, list[tuple[str, int, int]]] = {}
        direct_calls: dict[str, list[tuple[int, str]]] = {}
        direct_wait_deletes: dict[str, list[int]] = {}

        for key, (_, fnode) in callables.items():
            dlocks: list[tuple[str, int, int]] = []
            dcalls: list[tuple[int, str]] = []
            dwaits: list[int] = []
            for sub in _iter_no_nested_defs(fnode):
                if isinstance(sub, ast.With):
                    for item in sub.items:
                        lk = _lock_name_from_withitem(item)
                        if lk in rank_map:
                            dlocks.append((lk, rank_map[lk], sub.lineno))
                elif isinstance(sub, ast.Call):
                    ch = _attr_chain(sub.func)
                    if ch == "self._wait_active_deletes":
                        dwaits.append(sub.lineno)
                    if ch and (key, ch) in resolve_call:
                        dcalls.append((sub.lineno, resolve_call[(key, ch)]))
            direct_locks[key] = dlocks
            direct_calls[key] = dcalls
            direct_wait_deletes[key] = dwaits

        def reachable_locks(start_key: str) -> list[tuple[str, int, str, int]]:
            out: list[tuple[str, int, str, int]] = []
            visited: set[str] = set()
            stack = [start_key]
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                for lk, rk, line in direct_locks.get(cur, []):
                    out.append((lk, rk, cur, line))
                for _, nxt in direct_calls.get(cur, []):
                    if nxt not in visited:
                        stack.append(nxt)
            return out

        def reaches_wait_active_deletes(start_key: str) -> tuple[str, int] | None:
            visited: set[str] = set()
            stack = [start_key]
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                if direct_wait_deletes.get(cur):
                    return cur, direct_wait_deletes[cur][0]
                for _, nxt in direct_calls.get(cur, []):
                    if nxt not in visited:
                        stack.append(nxt)
            return None

        def walk_stmts(
            key: str,
            rel_path: str,
            stmts: list[ast.stmt],
            held_stack: list[tuple[str, int, int]],
        ) -> None:
            for stmt in stmts:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(stmt, ast.With):
                    new_locks: list[tuple[str, int, int]] = []
                    for item in stmt.items:
                        lk = _lock_name_from_withitem(item)
                        if lk in rank_map:
                            rk = rank_map[lk]
                            for prev_lk, prev_rk, prev_line in held_stack + new_locks:
                                if rk < prev_rk or (rk == prev_rk and lk != prev_lk):
                                    findings.append(
                                        Finding(
                                            check="LOCK_HIERARCHY_AND_GUARD",
                                            severity="CRITICAL",
                                            invariant="INV-LOCK-ORDER-1",
                                            rel_path=rel_path,
                                            line=stmt.lineno,
                                            symbol=key,
                                            message=(
                                                f"Lock order inversion: acquires `{lk}` (rank "
                                                f"{rk}) while holding "
                                                f"`{prev_lk}` (rank {prev_rk}, acquired at line "
                                                f"{prev_line})."
                                            ),
                                        )
                                    )
                            new_locks.append((lk, rk, stmt.lineno))
                    combined = held_stack + new_locks
                    walk_stmts(key, rel_path, stmt.body, combined)
                else:
                    if held_stack:
                        for sub in [stmt] + list(_iter_no_nested_defs(stmt)):
                            if isinstance(sub, ast.With):
                                continue
                            if isinstance(sub, ast.Call):
                                ch = _attr_chain(sub.func)
                                if (
                                    check_wait_active_deletes
                                    and ch == "self._wait_active_deletes"
                                    and any(lk == "self._lifecycle_lock" for lk, _, _ in held_stack)
                                ):
                                    findings.append(
                                        Finding(
                                            check="LOCK_HIERARCHY_AND_GUARD",
                                            severity="CRITICAL",
                                            invariant="INV-LOCK-ORDER-2",
                                            rel_path=rel_path,
                                            line=sub.lineno,
                                            symbol=key,
                                            message=(
                                                "`self._wait_active_deletes()` runs inside "
                                                "`self._lifecycle_lock`, "
                                                "which deadlocks against in-flight member deletes."
                                            ),
                                        )
                                    )
                                if ch and (key, ch) in resolve_call:
                                    callee_key = resolve_call[(key, ch)]
                                    for clk, crk, csym, cline in reachable_locks(callee_key):
                                        for prev_lk, prev_rk, prev_line in held_stack:
                                            if crk < prev_rk or (crk == prev_rk and clk != prev_lk):
                                                findings.append(
                                                    Finding(
                                                        check="LOCK_HIERARCHY_AND_GUARD",
                                                        severity="CRITICAL",
                                                        invariant="INV-LOCK-ORDER-1",
                                                        rel_path=rel_path,
                                                        line=sub.lineno,
                                                        symbol=key,
                                                        message=(
                                                            f"Transitive lock order inversion: "
                                                            f"call `{ch}()` while "
                                                            f"holding `{prev_lk}` (rank {prev_rk}, "
                                                            f"line {prev_line}) "
                                                            f"acquires `{clk}` (rank {crk}) in "
                                                            f"`{csym}:{cline}`."
                                                        ),
                                                    )
                                                )
                                    if check_wait_active_deletes and any(
                                        lk == "self._lifecycle_lock" for lk, _, _ in held_stack
                                    ):
                                        hit_wait = reaches_wait_active_deletes(callee_key)
                                        if hit_wait is not None:
                                            wsym, wline = hit_wait
                                            findings.append(
                                                Finding(
                                                    check="LOCK_HIERARCHY_AND_GUARD",
                                                    severity="CRITICAL",
                                                    invariant="INV-LOCK-ORDER-2",
                                                    rel_path=rel_path,
                                                    line=sub.lineno,
                                                    symbol=key,
                                                    message=(
                                                        f"Call `{ch}()` inside "
                                                        f"`self._lifecycle_lock` transitively "
                                                        f"invokes `_wait_active_deletes()` at "
                                                        f"`{wsym}:{wline}`."
                                                    ),
                                                )
                                            )
                    for field_val in ast.iter_child_nodes(stmt):
                        if isinstance(field_val, ast.stmt):
                            walk_stmts(key, rel_path, [field_val], held_stack)
                        elif isinstance(field_val, ast.ExceptHandler):
                            walk_stmts(key, rel_path, field_val.body, held_stack)

        for key, (rel_path, fnode) in callables.items():
            walk_stmts(key, rel_path, fnode.body, [])

    sb_rel = f"{SDK_REL}/sandbox_batch.py"
    sc_rel = f"{SDK_REL}/sandbox_client.py"
    sb_parsed = _parse_if_exists(repo_root, sb_rel)
    sc_parsed = _parse_if_exists(repo_root, sc_rel)
    if sb_parsed is not None and sc_parsed is not None:
        sb_methods = _class_methods(sb_parsed[0]).get("SandboxBatch", {})
        sc_methods = _class_methods(sc_parsed[0]).get("SandboxClient", {})
        sdk_callables: dict[str, tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for m, n in sb_methods.items():
            sdk_callables[f"SandboxBatch.{m}"] = (sb_rel, n)
        for m, n in sc_methods.items():
            sdk_callables[f"SandboxClient.{m}"] = (sc_rel, n)

        sdk_resolve: dict[tuple[str, str], str] = {}
        for m in sb_methods:
            for target in sb_methods:
                sdk_resolve[(f"SandboxBatch.{m}", f"self.{target}")] = f"SandboxBatch.{target}"
            for target in sc_methods:
                sdk_resolve[(f"SandboxBatch.{m}", f"self._client.{target}")] = (
                    f"SandboxClient.{target}"
                )
        for m in sc_methods:
            for target in sc_methods:
                sdk_resolve[(f"SandboxClient.{m}", f"self.{target}")] = f"SandboxClient.{target}"
                sdk_resolve[(f"SandboxClient.{m}", f"SandboxClient.{target}")] = (
                    f"SandboxClient.{target}"
                )

        sdk_ranks = {
            "self._lifecycle_lock": 1,
            "self._client._batches_lock": 2,
            "self._batches_lock": 2,
            "batches_lock": 2,
            "self._lock": 3,
            "self._changed": 3,
            "batch._lock": 3,
        }
        audit_sync_lock_graph(sdk_callables, sdk_ranks, sdk_resolve, check_wait_active_deletes=True)

        # Check whether target SandboxBatch helpers acquire self._lock internally.
        self_locked_sb_helpers: set[str] = set()
        for hname in ("_detach_unfinished", "_exit_detaches"):
            hnode = sb_methods.get(hname)
            if hnode is not None and any(
                isinstance(st, ast.With)
                and any(_lock_name_from_withitem(it) == "self._lock" for it in st.items)
                for st in _iter_no_nested_defs(hnode)
            ):
                self_locked_sb_helpers.add(f"batch.{hname}")

        # Audit unlocked reads of lock-guarded SandboxBatch handle state in SandboxClient.
        for mname, mnode in sc_methods.items():
            guarded_node_ids: set[int] = set()
            for sub in _iter_no_nested_defs(mnode):
                if isinstance(sub, ast.With):
                    if any(_lock_name_from_withitem(it) == "batch._lock" for it in sub.items):
                        for inner in _iter_no_nested_defs(sub):
                            guarded_node_ids.add(id(inner))
            for sub in _iter_no_nested_defs(mnode):
                if id(sub) in guarded_node_ids:
                    continue
                if (
                    isinstance(sub, ast.Call)
                    and _attr_chain(sub.func)
                    in {"batch._detach_unfinished", "batch._exit_detaches"}
                    and _attr_chain(sub.func) not in self_locked_sb_helpers
                ):
                    findings.append(
                        Finding(
                            check="LOCK_HIERARCHY_AND_GUARD",
                            severity="LOW",
                            invariant="INV-PARITY-LOCK-1",
                            rel_path=sc_rel,
                            line=sub.lineno,
                            symbol=f"SandboxClient.{mname}",
                            message=(
                                f"Calls `{_attr_chain(sub.func)}()` without holding `batch._lock`, "
                                f"reading `_detach_grace` and `_lease_released` unlocked while "
                                f"concurrent `detach()` / `release()` mutate both fields under "
                                f"`batch._lock`."
                            ),
                        )
                    )
                elif isinstance(sub, ast.Attribute) and _attr_chain(sub) in {
                    "batch._connected",
                    "batch._stale_connections",
                }:
                    findings.append(
                        Finding(
                            check="LOCK_HIERARCHY_AND_GUARD",
                            severity="LOW",
                            invariant="INV-PARITY-LOCK-1",
                            rel_path=sc_rel,
                            line=sub.lineno,
                            symbol=f"SandboxClient.{mname}",
                            message=(
                                f"Reads `{_attr_chain(sub)}` without holding `batch._lock` while "
                                f"concurrent "
                                f"`connect()`, `_watch_loop()`, or `_close_cached_connections()` "
                                f"mutate "
                                f"connection tables under `batch._lock`."
                            ),
                        )
                    )

    bc_rel = f"{RL_REL}/batch_claims.py"
    bc_parsed = _parse_if_exists(repo_root, bc_rel)
    if bc_parsed is not None:
        bc_methods = _class_methods(bc_parsed[0]).get("BatchClaims", {})
        bc_callables = {f"BatchClaims.{m}": (bc_rel, n) for m, n in bc_methods.items()}
        bc_resolve = {
            (f"BatchClaims.{m}", f"self.{t}"): f"BatchClaims.{t}"
            for m in bc_methods
            for t in bc_methods
        }
        bc_ranks = {
            "self._cluster_lock()": 1,
            "self._lock": 2,
        }
        audit_sync_lock_graph(bc_callables, bc_ranks, bc_resolve, check_wait_active_deletes=False)

    return findings


# ---------------------------------------------------------------------------
# Check 4: Outbound K8s request timeout and unretried batch client routing
# ---------------------------------------------------------------------------


def _kw_is_non_none(call: ast.Call, name: str) -> bool:
    for kw in call.keywords:
        if kw.arg == name:
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


def _kw_is_true(call: ast.Call, name: str) -> bool:
    for kw in call.keywords:
        if kw.arg == name:
            return isinstance(kw.value, ast.Constant) and kw.value.value is True
    return False


def check_k8s_timeout_and_client(repo_root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []

    kh_rel = f"{SDK_REL}/k8s_helper.py"
    kh_parsed = _parse_if_exists(repo_root, kh_rel)
    if kh_parsed is not None:
        kh_methods = _class_methods(kh_parsed[0]).get("K8sHelper", {})
        batch_only_kh_methods = {
            "list_sandbox_claim_objects",
            "count_live_sandbox_claims",
            "delete_sandbox_claims_by_label",
            "get_sandbox_warmpool",
            "get_sandbox_template",
        }
        for mname in sorted(batch_only_kh_methods):
            mnode = kh_methods.get(mname)
            if mnode is None:
                continue
            for sub in _iter_no_nested_defs(mnode):
                if isinstance(sub, ast.Attribute) and _attr_chain(sub) == "self.custom_objects_api":
                    findings.append(
                        Finding(
                            check="K8S_TIMEOUT_AND_CLIENT",
                            severity="HIGH",
                            invariant="INV-OWN-TIMEOUT-1",
                            rel_path=kh_rel,
                            line=sub.lineno,
                            symbol=f"K8sHelper.{mname}",
                            message=(
                                f"Batch helper method `{mname}` uses retried "
                                f"`self.custom_objects_api` "
                                f"instead of unretried `self._batch_custom_objects_api`."
                            ),
                        )
                    )

    helper_k8s_methods = {
        "create_sandbox_claim",
        "delete_sandbox_claim",
        "list_sandbox_claim_objects",
        "count_live_sandbox_claims",
        "delete_sandbox_claims_by_label",
        "watch_sandbox_claims",
        "get_sandbox_warmpool",
        "get_sandbox_template",
        "create_batch_lease",
        "read_batch_lease",
        "replace_batch_lease",
        "delete_batch_lease",
    }

    # Verify both K8sHelper and AsyncK8sHelper accept `_request_timeout` on every batch helper.
    for helper_rel, helper_cls in [
        (f"{SDK_REL}/k8s_helper.py", "K8sHelper"),
        (f"{SDK_REL}/async_k8s_helper.py", "AsyncK8sHelper"),
    ]:
        hparsed = _parse_if_exists(repo_root, helper_rel)
        if hparsed is None:
            continue
        hmethods = _class_methods(hparsed[0]).get(helper_cls, {})
        for mname in sorted(helper_k8s_methods):
            mnode = hmethods.get(mname)
            if mnode is None:
                continue
            all_arg_names = {
                a.arg for a in (*mnode.args.posonlyargs, *mnode.args.args, *mnode.args.kwonlyargs)
            }
            if "_request_timeout" not in all_arg_names:
                findings.append(
                    Finding(
                        check="K8S_TIMEOUT_AND_CLIENT",
                        severity="HIGH",
                        invariant="INV-OWN-TIMEOUT-2",
                        rel_path=helper_rel,
                        line=mnode.lineno,
                        symbol=f"{helper_cls}.{mname}",
                        message=f"`{helper_cls}.{mname}` does not accept `_request_timeout`.",
                    )
                )

    raw_k8s_api_methods = {
        "list_namespaced_lease",
        "delete_namespaced_lease",
        "create_namespaced_lease",
        "read_namespaced_lease",
        "replace_namespaced_lease",
        "create_namespaced_custom_object",
        "delete_namespaced_custom_object",
        "delete_collection_namespaced_custom_object",
        "list_namespaced_custom_object",
        "get_namespaced_custom_object",
    }

    for rel_path in [
        f"{SDK_REL}/sandbox_batch.py",
        f"{SDK_REL}/async_sandbox_batch.py",
        f"{SDK_REL}/batch_reaper.py",
    ]:
        parsed = _parse_if_exists(repo_root, rel_path)
        if parsed is None:
            continue
        tree, _ = parsed
        for sub in ast.walk(tree):
            if not isinstance(sub, ast.Call):
                continue
            chain = _attr_chain(sub.func)
            if not chain:
                continue
            tail = chain.split(".")[-1]
            if tail not in helper_k8s_methods and tail not in raw_k8s_api_methods:
                continue
            has_star_kwargs = any(kw.arg is None for kw in sub.keywords)
            has_timeout = _kw_is_non_none(sub, "_request_timeout") or _kw_is_non_none(
                sub, "deadline"
            )
            if not has_timeout and not has_star_kwargs:
                findings.append(
                    Finding(
                        check="K8S_TIMEOUT_AND_CLIENT",
                        severity="HIGH",
                        invariant="INV-OWN-TIMEOUT-2",
                        rel_path=rel_path,
                        line=sub.lineno,
                        symbol=chain,
                        message=(
                            f"Outbound Kubernetes call `{chain}(...)` omits non-None "
                            f"`_request_timeout` "
                            f"and `deadline`, risking an unbounded socket read on a batch or "
                            f"Lease-fenced path."
                        ),
                    )
                )
            if (
                rel_path.endswith("sandbox_batch.py")
                and tail in {"create_sandbox_claim", "delete_sandbox_claim"}
                and not _kw_is_true(sub, "batch")
            ):
                findings.append(
                    Finding(
                        check="K8S_TIMEOUT_AND_CLIENT",
                        severity="HIGH",
                        invariant="INV-OWN-TIMEOUT-1",
                        rel_path=rel_path,
                        line=sub.lineno,
                        symbol=chain,
                        message=(
                            f"Batch call `{chain}(...)` omits `batch=True`, routing through the "
                            f"retried "
                            f"`custom_objects_api` client instead of unretried "
                            f"`_batch_custom_objects_api`."
                        ),
                    )
                )

        # Check `_delete_claims_in_rounds` clamps collection timeout/deadline to remaining Lease
        # lifetime.
        mod_funcs = _module_functions(tree)
        del_rounds = mod_funcs.get("_delete_claims_in_rounds")
        if del_rounds is not None:
            fn_src = ast.unparse(del_rounds)
            clamps_budget = "min(" in fn_src or "deadline=" in fn_src
            if not clamps_budget:
                findings.append(
                    Finding(
                        check="K8S_TIMEOUT_AND_CLIENT",
                        severity="CRITICAL",
                        invariant="INV-OWN-3",
                        rel_path=rel_path,
                        line=del_rounds.lineno,
                        symbol="_delete_claims_in_rounds",
                        message=(
                            "`_delete_claims_in_rounds` issues `delete_sandbox_claims_by_label` "
                            "with unclamped "
                            "`BATCH_COLLECTION_REQUEST_TIMEOUT_SECONDS` (90s) and paginated "
                            "`count_live_sandbox_claims` (which lacks a `deadline` parameter and "
                            "forwards "
                            "`deadline=None` to `_claim_list_pages`), allowing `release_group()` "
                            "to mutate and "
                            "list claims after `_lease_expires_at` passes."
                        ),
                    )
                )

    # Check AsyncK8sHelper.watch_sandbox_claims validates HTTP response status on
    # _preload_content=False stream.
    akh_rel = f"{SDK_REL}/async_k8s_helper.py"
    akh_parsed = _parse_if_exists(repo_root, akh_rel)
    if akh_parsed is not None:
        akh_methods = _class_methods(akh_parsed[0]).get("AsyncK8sHelper", {})
        akh_funcs = _module_functions(akh_parsed[0])
        wsc = akh_methods.get("watch_sandbox_claims")
        if wsc is not None:
            combined_nodes: list[ast.AST] = [wsc]
            for sub in ast.walk(wsc):
                if isinstance(sub, ast.Call):
                    ch = _attr_chain(sub.func)
                    if ch and ch.startswith("self.") and ch.count(".") == 1:
                        callee = akh_methods.get(ch.split(".", 1)[1])
                        if callee is not None:
                            combined_nodes.append(callee)
                    elif ch and ch in akh_funcs:
                        combined_nodes.append(akh_funcs[ch])
            wsc_src = "\n".join(ast.unparse(n) for n in combined_nodes)
            if ".status" not in wsc_src and "_raise_for_status" not in wsc_src:
                findings.append(
                    Finding(
                        check="K8S_TIMEOUT_AND_CLIENT",
                        severity="HIGH",
                        invariant="INV-WATCH-HTTP-1",
                        rel_path=akh_rel,
                        line=wsc.lineno,
                        symbol="AsyncK8sHelper.watch_sandbox_claims",
                        message=(
                            "`watch_sandbox_claims` streams via "
                            "`kubernetes_asyncio.watch.Watch.stream()` "
                            "(`_preload_content=False`) without checking `200 <= resp.status <= "
                            "299`; non-JSON or "
                            "multi-line HTTP 401/403/429/502/503 responses return cleanly as "
                            "0-event watches and "
                            "single-line 429 JSON responses drop `Retry-After` headers."
                        ),
                    )
                )

    for rl_rel_file in [f"{RL_REL}/batch_claims.py", f"{RL_REL}/fleet.py"]:
        rl_parsed = _parse_if_exists(repo_root, rl_rel_file)
        if rl_parsed is None:
            continue
        for node in rl_parsed[0].body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for sub in _iter_no_nested_defs(item):
                            if not isinstance(sub, ast.Call):
                                continue
                            chain = _attr_chain(sub.func)
                            if chain and chain.endswith("resources.delete_claim"):
                                if not _kw_is_non_none(sub, "request_timeout"):
                                    findings.append(
                                        Finding(
                                            check="K8S_TIMEOUT_AND_CLIENT",
                                            severity="MEDIUM",
                                            invariant="INV-OWN-TIMEOUT-3",
                                            rel_path=rl_rel_file,
                                            line=sub.lineno,
                                            symbol=f"{node.name}.{item.name}",
                                            message=(
                                                f"`{chain}(...)` omits non-None `request_timeout` "
                                                f"(defaults to "
                                                f"`None` on retried `custom_api` in "
                                                f"`Resources.delete_claim`, "
                                                f"whereas `failfast.py` passes `_REQUEST_TIMEOUT = "
                                                f"(3, 10)` and SDK "
                                                f"batch deletes use bounded timeouts)."
                                            ),
                                        )
                                    )
    return findings


# ---------------------------------------------------------------------------
# Check 5: BaseException / cancellation rollback and teardown tracking safety
# ---------------------------------------------------------------------------


def _handler_catches_base_exception(h: ast.ExceptHandler) -> bool:
    return h.type is None or "BaseException" in ast.unparse(h.type)


def _block_releases_batch_var(stmts: list[ast.stmt], var_name: str) -> bool:
    for st in stmts:
        for sub in [st] + list(_iter_no_nested_defs(st)):
            if isinstance(sub, ast.Call):
                ch = _attr_chain(sub.func)
                if ch in {f"{var_name}.release", "self.release_all"}:
                    return True
    return False


def _stmt_registers_opened(stmt: ast.stmt, var_name: str) -> bool:
    for sub in [stmt] + list(_iter_no_nested_defs(stmt)):
        if isinstance(sub, ast.Call) and _attr_chain(sub.func) == "self._opened.append":
            if sub.args and isinstance(sub.args[0], ast.Name) and sub.args[0].id == var_name:
                return True
    return False


def check_base_exception_rollback(repo_root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []

    # 5a. Sync SandboxFleet rollback handlers on BaseException (SIGINT/SIGTERM _on_signal).
    fl_rel = f"{RL_REL}/fleet.py"
    fl_parsed = _parse_if_exists(repo_root, fl_rel)
    if fl_parsed is not None:
        for cls_name, methods in _class_methods(fl_parsed[0]).items():
            for mname in ("acquire", "_acquire_reserved", "release", "resume", "restore"):
                mnode = methods.get(mname)
                if mnode is None:
                    continue
                for sub in _iter_no_nested_defs(mnode):
                    if not isinstance(sub, ast.Try):
                        continue
                    catches_only_exception = False
                    catches_base = False
                    handler_line = sub.lineno
                    rollback_summary: list[str] = []
                    for h in sub.handlers:
                        if _handler_catches_base_exception(h):
                            catches_base = True
                        elif h.type is not None and ast.unparse(h.type) == "Exception":
                            has_reraise = any(
                                isinstance(st, ast.Raise) for st in _iter_no_nested_defs(h)
                            )
                            if not has_reraise:
                                continue
                            catches_only_exception = True
                            handler_line = h.lineno
                            for st in _iter_no_nested_defs(h):
                                if isinstance(st, (ast.Assign, ast.AugAssign)):
                                    targets_txt = (
                                        ast.unparse(st.target)
                                        if isinstance(st, ast.AugAssign)
                                        else " ".join(ast.unparse(t) for t in st.targets)
                                    )
                                    if "_claims_reserved" in targets_txt:
                                        rollback_summary.append("_claims_reserved decrement")
                                elif isinstance(st, ast.Call):
                                    ch = _attr_chain(st.func)
                                    if ch and any(
                                        tok in ch
                                        for tok in (
                                            "claim.release",
                                            "delete_claim",
                                            "_release_on_demand",
                                            "_handles.append",
                                            "_free_slot_for_suspend",
                                        )
                                    ):
                                        rollback_summary.append(f"{ch}()")
                    final_txt = " ".join(ast.unparse(f) for f in sub.finalbody)
                    unguarded_rollbacks = [
                        r
                        for r in sorted(set(rollback_summary))
                        if r.removesuffix("()").removesuffix(" decrement") not in final_txt
                    ]
                    if catches_only_exception and not catches_base and unguarded_rollbacks:
                        findings.append(
                            Finding(
                                check="BASE_EXCEPTION_ROLLBACK",
                                severity="HIGH",
                                invariant="INV-LEAK-2",
                                rel_path=fl_rel,
                                line=handler_line,
                                symbol=f"{cls_name}.{mname}",
                                message=(
                                    f"Rollback handler uses `except Exception:` without "
                                    f"`BaseException`, "
                                    f"bypassing rollback ({', '.join(unguarded_rollbacks)}) "
                                    f"when `KeyboardInterrupt` (raised by `_on_signal` on "
                                    f"SIGINT/SIGTERM) "
                                    f"interrupts `{mname}`."
                                ),
                            )
                        )

    # 5b. AsyncSandboxFleet.acquire cancellation across worker-thread boundary.
    afl_rel = f"{RL_REL}/async_fleet.py"
    afl_parsed = _parse_if_exists(repo_root, afl_rel)
    if afl_parsed is not None:
        afl_methods = _class_methods(afl_parsed[0]).get("AsyncSandboxFleet", {})
        afl_acq = afl_methods.get("acquire")
        if afl_acq is not None:
            inspected_nodes: list[ast.AST] = list(_iter_no_nested_defs(afl_acq))
            for sub in _iter_no_nested_defs(afl_acq):
                if isinstance(sub, ast.Call):
                    ch = _attr_chain(sub.func)
                    if ch and ch.startswith("self.") and ch.count(".") == 1:
                        helper_m = afl_methods.get(ch.split(".", 1)[1])
                        if helper_m is not None:
                            inspected_nodes.extend(_iter_no_nested_defs(helper_m))
            has_cancel_handler = any(
                isinstance(sub, ast.Try)
                and (
                    bool(sub.finalbody)
                    or any(
                        _handler_catches_base_exception(h)
                        or (h.type is not None and "CancelledError" in ast.unparse(h.type))
                        for h in sub.handlers
                    )
                )
                for sub in inspected_nodes
            )
            uses_executor_submit_or_callback = any(
                isinstance(sub, ast.Call)
                and (_attr_chain(sub.func) or "").endswith(("submit", "add_done_callback"))
                for sub in inspected_nodes
            )
            if not (has_cancel_handler and uses_executor_submit_or_callback):
                findings.append(
                    Finding(
                        check="BASE_EXCEPTION_ROLLBACK",
                        severity="HIGH",
                        invariant="INV-LEAK-2",
                        rel_path=afl_rel,
                        line=afl_acq.lineno,
                        symbol="AsyncSandboxFleet.acquire",
                        message=(
                            "`AsyncSandboxFleet.acquire` delegates to a worker thread without "
                            "attaching a "
                            "`concurrent.futures.Future` (`self._thread_pool().submit(...)`) "
                            "completion "
                            "callback on cancellation (`BaseException` / `CancelledError`); "
                            "cancelling "
                            "`await fleet.acquire(task)` leaves the worker thread running and "
                            "orphans the "
                            "returned `SandboxHandle`, `BatchClaim`, and `_claims_reserved`."
                        ),
                    )
                )

    # 5c & 5d. BatchClaims._batch_for and BatchClaims.release_all untracked live SandboxBatch
    # handles.
    bc_rel = f"{RL_REL}/batch_claims.py"
    bc_parsed = _parse_if_exists(repo_root, bc_rel)
    if bc_parsed is not None:
        bc_methods = _class_methods(bc_parsed[0]).get("BatchClaims", {})

        batch_for = bc_methods.get("_batch_for")
        if batch_for is not None:

            def audit_stmt_list_for_claim_batch(
                stmts: list[ast.stmt], inside_protective_try: str | None
            ) -> None:
                for idx, stmt in enumerate(stmts):
                    claim_var: str | None = None
                    if (
                        isinstance(stmt, ast.Assign)
                        and len(stmt.targets) == 1
                        and isinstance(stmt.targets[0], ast.Name)
                        and isinstance(stmt.value, ast.Call)
                    ):
                        ch = _attr_chain(stmt.value.func)
                        if ch and ch.endswith("claim_batch"):
                            claim_var = stmt.targets[0].id
                    if claim_var is not None:
                        protected = inside_protective_try == claim_var
                        rem = [
                            s
                            for s in stmts[idx + 1 :]
                            if not (
                                isinstance(s, (ast.Assign, ast.AnnAssign))
                                and not any(isinstance(n, ast.Call) for n in ast.walk(s))
                            )
                        ]
                        if not protected and rem:
                            nxt = rem[0]
                            if isinstance(nxt, ast.Try):
                                releases_on_base = _block_releases_batch_var(
                                    nxt.finalbody, claim_var
                                ) or any(
                                    _handler_catches_base_exception(h)
                                    and _block_releases_batch_var(h.body, claim_var)
                                    for h in nxt.handlers
                                )
                                registers_first = bool(nxt.body) and _stmt_registers_opened(
                                    nxt.body[0], claim_var
                                )
                                if releases_on_base or registers_first:
                                    protected = True
                            elif (
                                isinstance(nxt, ast.With)
                                and bool(nxt.body)
                                and _stmt_registers_opened(nxt.body[0], claim_var)
                            ):
                                protected = True
                        if not protected:
                            findings.append(
                                Finding(
                                    check="BASE_EXCEPTION_ROLLBACK",
                                    severity="HIGH",
                                    invariant="INV-LEAK-1",
                                    rel_path=bc_rel,
                                    line=stmt.lineno,
                                    symbol="BatchClaims._batch_for",
                                    message=(
                                        "`claim_batch()` opens a live renewing `SandboxBatch` "
                                        "outside `self._lock` "
                                        "without unconditional registration in `self._opened` or "
                                        "`BaseException` "
                                        "`batch.release()` rollback; if `self._closed` is True and "
                                        "`batch.release()` "
                                        "fails with a transient 503 (or `KeyboardInterrupt` "
                                        "arrives before registration), "
                                        "the batch is excluded from `self._opened` and cannot be "
                                        "retried by `release_all()`."
                                    ),
                                )
                            )
                    elif isinstance(stmt, ast.With):
                        audit_stmt_list_for_claim_batch(stmt.body, inside_protective_try)
                    elif isinstance(stmt, ast.If):
                        audit_stmt_list_for_claim_batch(stmt.body, inside_protective_try)
                        audit_stmt_list_for_claim_batch(stmt.orelse, inside_protective_try)
                    elif isinstance(stmt, ast.Try):
                        prot_var: str | None = inside_protective_try
                        for inner in _iter_no_nested_defs(stmt):
                            if (
                                isinstance(inner, ast.Assign)
                                and len(inner.targets) == 1
                                and isinstance(inner.targets[0], ast.Name)
                                and isinstance(inner.value, ast.Call)
                            ):
                                ch = _attr_chain(inner.value.func)
                                if ch and ch.endswith("claim_batch"):
                                    cvar = inner.targets[0].id
                                    if _block_releases_batch_var(stmt.finalbody, cvar) or any(
                                        _handler_catches_base_exception(h)
                                        and _block_releases_batch_var(h.body, cvar)
                                        for h in stmt.handlers
                                    ):
                                        prot_var = cvar
                        audit_stmt_list_for_claim_batch(stmt.body, prot_var)

            audit_stmt_list_for_claim_batch(batch_for.body, None)

        release_all = bc_methods.get("release_all")
        if release_all is not None:
            clear_lines: list[int] = []
            release_lines: list[int] = []
            retains_failed = False
            for sub in _iter_no_nested_defs(release_all):
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if isinstance(tgt, (ast.Tuple, ast.List)) and isinstance(
                            sub.value, (ast.Tuple, ast.List)
                        ):
                            for l_elt, r_elt in zip(tgt.elts, sub.value.elts):
                                if "self._opened" in ast.unparse(l_elt):
                                    if ast.unparse(r_elt).strip() == "[]":
                                        clear_lines.append(sub.lineno)
                                    else:
                                        retains_failed = True
                        elif "self._opened" in ast.unparse(tgt):
                            if ast.unparse(sub.value).strip() == "[]":
                                clear_lines.append(sub.lineno)
                            else:
                                retains_failed = True
                elif isinstance(sub, ast.Call):
                    ch = _attr_chain(sub.func)
                    if ch == "self._opened.clear":
                        clear_lines.append(sub.lineno)
                    elif ch in {"self._opened.append", "self._opened.extend"}:
                        retains_failed = True
                    elif ch and ch.endswith(".release"):
                        release_lines.append(sub.lineno)
            if (
                clear_lines
                and release_lines
                and min(clear_lines) < min(release_lines)
                and not retains_failed
            ):
                findings.append(
                    Finding(
                        check="BASE_EXCEPTION_ROLLBACK",
                        severity="MEDIUM",
                        invariant="INV-LEAK-1",
                        rel_path=bc_rel,
                        line=min(clear_lines),
                        symbol="BatchClaims.release_all",
                        message=(
                            "`self._opened` is cleared upfront before iterating `batch.release()`; "
                            "a transient "
                            "failure or `KeyboardInterrupt` mid-loop permanently drops unreleased "
                            "`SandboxBatch` "
                            "handles from `self._opened`, making subsequent `release_all()` "
                            "retries a no-op."
                        ),
                    )
                )

    # 5e. AsyncSandboxClient._stop_batches unconditionally unregistering failed-release batches on
    # __aexit__.
    asc_rel = f"{SDK_REL}/async_sandbox_client.py"
    asc_parsed = _parse_if_exists(repo_root, asc_rel)
    if asc_parsed is not None:
        stop_batches = (
            _class_methods(asc_parsed[0]).get("AsyncSandboxClient", {}).get("_stop_batches")
        )
        if stop_batches is not None:
            for st in stop_batches.body:
                if isinstance(st, ast.For):
                    for bst in st.body:
                        if isinstance(bst, ast.Expr) and isinstance(bst.value, ast.Call):
                            if _attr_chain(bst.value.func) == "self._unregister_batch":
                                findings.append(
                                    Finding(
                                        check="BASE_EXCEPTION_ROLLBACK",
                                        severity="HIGH",
                                        invariant="INV-LEAK-1",
                                        rel_path=asc_rel,
                                        line=bst.lineno,
                                        symbol="AsyncSandboxClient._stop_batches",
                                        message=(
                                            "`_stop_batches()` unconditionally calls "
                                            "`self._unregister_batch(batch)` "
                                            "at the end of the loop; when `__aexit__` calls "
                                            "`close()` in `finally:` "
                                            "after `_delete_automatic_sandboxes()` failed to "
                                            "`release()` a batch, "
                                            "the batch is unregistered before `_atexit_cleanup` or "
                                            "`delete_all()` can retry."
                                        ),
                                    )
                                )

    return findings


# ---------------------------------------------------------------------------
# Check 6: Normalized Sync-vs-Async AST Surface & Cancellation-Block Ordering
# ---------------------------------------------------------------------------


_STATE_ROLLBACK_TOKENS = (
    "self._state.",
    "self._acquire_waiters.pop",
    "self._prechecks",
    "self._active_deletes",
    "self._connected",
    "self._stale_connections",
)


def _stmt_has_state_mutation(stmt: ast.stmt) -> bool:
    for node in [stmt] + list(_iter_no_nested_defs(stmt)):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Call)):
            txt = ast.unparse(node)
            if any(tok in txt for tok in _STATE_ROLLBACK_TOKENS):
                return True
    return False


def _stmt_first_yield(stmt: ast.stmt) -> tuple[int, str, bool] | None:
    """Returns ``(lineno, expr, is_lock_only)`` for the first yield in ``stmt``, if any."""
    if isinstance(stmt, ast.AsyncWith):
        locks = [_lock_name_from_withitem(it) for it in stmt.items]
        is_lock_only = all(lk in {"self._lock", "self._changed"} for lk in locks)
        return (
            stmt.lineno,
            f"async with {', '.join(ast.unparse(it.context_expr) for it in stmt.items)}",
            is_lock_only,
        )
    if isinstance(stmt, ast.AsyncFor):
        return stmt.lineno, "async for", False
    for node in [stmt] + list(_iter_no_nested_defs(stmt)):
        if isinstance(node, ast.Await):
            return node.lineno, ast.unparse(node), False
        if isinstance(node, ast.AsyncWith):
            locks = [_lock_name_from_withitem(it) for it in node.items]
            is_lock_only = all(lk in {"self._lock", "self._changed"} for lk in locks)
            return (
                node.lineno,
                f"async with {', '.join(ast.unparse(it.context_expr) for it in node.items)}",
                is_lock_only,
            )
        if isinstance(node, ast.AsyncFor):
            return node.lineno, "async for", False
    return None


def check_sync_async_ast_parity(repo_root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []
    sync_rel = f"{SDK_REL}/sandbox_batch.py"
    async_rel = f"{SDK_REL}/async_sandbox_batch.py"
    sync_parsed = _parse_if_exists(repo_root, sync_rel)
    async_parsed = _parse_if_exists(repo_root, async_rel)
    if sync_parsed is None or async_parsed is None:
        return findings

    sync_methods = _class_methods(sync_parsed[0]).get("SandboxBatch", {})
    async_methods = _class_methods(async_parsed[0]).get("AsyncSandboxBatch", {})

    sync_public = {
        m for m in sync_methods if not m.startswith("_") or m in ("__enter__", "__exit__")
    }
    async_public = {
        m for m in async_methods if not m.startswith("_") or m in ("__aenter__", "__aexit__")
    }
    allowed_sync_only = {"__enter__", "__exit__"}
    allowed_async_only = {"__aenter__", "__aexit__"}

    for m in sorted(sync_public - async_public - allowed_sync_only):
        findings.append(
            Finding(
                check="SYNC_ASYNC_AST_PARITY",
                severity="MEDIUM",
                invariant="INV-PARITY-1",
                rel_path=sync_rel,
                line=sync_methods[m].lineno,
                symbol=f"SandboxBatch.{m}",
                message=(
                    f"Public method `SandboxBatch.{m}` has no counterpart on `AsyncSandboxBatch`."
                ),
            )
        )
    for m in sorted(async_public - sync_public - allowed_async_only):
        findings.append(
            Finding(
                check="SYNC_ASYNC_AST_PARITY",
                severity="MEDIUM",
                invariant="INV-PARITY-1",
                rel_path=async_rel,
                line=async_methods[m].lineno,
                symbol=f"AsyncSandboxBatch.{m}",
                message=(
                    f"Public method `AsyncSandboxBatch.{m}` has no counterpart on `SandboxBatch`."
                ),
            )
        )

    def check_cleanup_seq(mname: str, label: str, stmts: list[ast.stmt]) -> None:
        first_yield: tuple[int, str, bool] | None = None

        def visit_seq(seq: list[ast.stmt], scope_name: str) -> bool:
            nonlocal first_yield
            for st in seq:
                if isinstance(st, ast.If):
                    if visit_seq(st.body, scope_name) or visit_seq(st.orelse, scope_name):
                        return True
                    continue
                if isinstance(st, ast.With):
                    if visit_seq(st.body, scope_name):
                        return True
                    continue
                if (
                    isinstance(st, ast.Expr)
                    and isinstance(st.value, ast.Await)
                    and isinstance(st.value.value, ast.Call)
                ):
                    ch = _attr_chain(st.value.value.func)
                    if ch and ch.startswith("self.") and ch.count(".") == 1:
                        hname = ch.split(".", 1)[1]
                        hnode = async_methods.get(hname)
                        if hnode is not None:
                            check_cleanup_seq(f"{scope_name} -> {hname}", label, hnode.body)
                            continue

                yld = _stmt_first_yield(st)
                if yld is not None and first_yield is None and not yld[2]:
                    first_yield = yld
                if _stmt_has_state_mutation(st) and first_yield is not None:
                    yline, yexpr, _ = first_yield
                    findings.append(
                        Finding(
                            check="SYNC_ASYNC_AST_PARITY",
                            severity="HIGH",
                            invariant="INV-PARITY-LOCK-2",
                            rel_path=async_rel,
                            line=yline,
                            symbol=f"AsyncSandboxBatch.{scope_name}",
                            message=(
                                f"`{label}` executes non-lock suspension point `{yexpr}` before "
                                f"completing "
                                f"shared-state rollback, allowing a second `CancelledError` to "
                                f"skip state cleanup."
                            ),
                        )
                    )
                    return True
            return False

        visit_seq(stmts, mname)

    for mname, am in async_methods.items():
        for sub in _iter_no_nested_defs(am):
            if not isinstance(sub, ast.Try):
                continue
            for h in sub.handlers:
                if h.type is None or any(
                    tok in ast.unparse(h.type) for tok in ("BaseException", "CancelledError")
                ):
                    check_cleanup_seq(
                        mname, f"except {ast.unparse(h.type) if h.type else ''}", h.body
                    )
            if sub.finalbody:
                check_cleanup_seq(mname, "finally", sub.finalbody)

    return findings


# ---------------------------------------------------------------------------
# Check 7: PEP 561 Public Export Completeness (`py.typed` + `__all__`)
# ---------------------------------------------------------------------------


def check_pep561_exports(repo_root: pathlib.Path) -> list[Finding]:
    findings: list[Finding] = []
    py_typed = repo_root / SDK_REL / "py.typed"
    init_rel = f"{SDK_REL}/__init__.py"
    parsed = _parse_if_exists(repo_root, init_rel)
    if not py_typed.exists() or parsed is None:
        return findings

    tree, _ = parsed
    exported_symbols: list[str] = []
    declared_all: set[str] | None = None

    def collect_top_bindings(stmts: list[ast.stmt]) -> None:
        nonlocal declared_all
        for st in stmts:
            if isinstance(st, ast.ImportFrom):
                for alias in st.names:
                    bound = alias.asname or alias.name
                    if not bound.startswith("_"):
                        exported_symbols.append(bound)
            elif isinstance(st, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if not st.name.startswith("_"):
                    exported_symbols.append(st.name)
            elif isinstance(st, ast.Try):
                collect_top_bindings(st.body)
                for h in st.handlers:
                    collect_top_bindings(h.body)
            elif isinstance(st, ast.Assign):
                for tgt in st.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                        if isinstance(st.value, (ast.List, ast.Tuple)):
                            declared_all = {
                                elt.value
                                for elt in st.value.elts
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                            }

    collect_top_bindings(tree.body)
    unique_public = sorted(set(exported_symbols))

    if declared_all is None:
        findings.append(
            Finding(
                check="PEP561_PUBLIC_EXPORTS",
                severity="MEDIUM",
                invariant="INV-TYPES-2",
                rel_path=init_rel,
                line=15,
                symbol="k8s_agent_sandbox.__all__",
                message=(
                    f"Package ships `py.typed` and imports {len(unique_public)} public symbols in "
                    f"`__init__.py` "
                    f"without defining `__all__`; strict PEP 561 consumers (`mypy --strict` / "
                    f"`no_implicit_reexport = true`) reject `from k8s_agent_sandbox import "
                    f"SandboxBatch, ...` "
                    f"with `[attr-defined]`."
                ),
            )
        )
    else:
        missing = sorted(set(unique_public) - declared_all)
        if missing:
            findings.append(
                Finding(
                    check="PEP561_PUBLIC_EXPORTS",
                    severity="MEDIUM",
                    invariant="INV-TYPES-2",
                    rel_path=init_rel,
                    line=15,
                    symbol="k8s_agent_sandbox.__all__",
                    message=(
                        f"`__init__.py` defines `__all__` but omits public symbol(s): "
                        f"{', '.join(missing)}."
                    ),
                )
            )
    return findings


def run_mypy_checks(repo_root: pathlib.Path, python_exe: str) -> list[Finding]:
    findings: list[Finding] = []
    sdk_dir = repo_root / "clients/python/agentic-sandbox-client"
    rl_dir = repo_root / "examples/agent-sandbox-rl"

    env = os.environ.copy()
    mypy_paths = [str(sdk_dir)]
    if rl_dir.exists():
        mypy_paths.append(str(rl_dir))
    if env.get("MYPYPATH"):
        mypy_paths.append(env["MYPYPATH"])
    env["MYPYPATH"] = ":".join(mypy_paths)

    targets: list[tuple[str, list[str]]] = [
        ("clients/python/agentic-sandbox-client", ["k8s_agent_sandbox"]),
    ]
    if (rl_dir / "agent_sandbox_rl").exists():
        targets.append((
            "examples/agent-sandbox-rl",
            ["--ignore-missing-imports", "agent_sandbox_rl"],
        ))
    if (rl_dir / "agent_sandbox_rl/batch_claims.py").exists():
        targets.append((
            "examples/agent-sandbox-rl",
            ["--strict", "--ignore-missing-imports", "agent_sandbox_rl/batch_claims.py"],
        ))

    seen: set[tuple[str, int, str]] = set()
    for cwd_rel, mypy_args in targets:
        proc = subprocess.run(
            [python_exe, "-m", "mypy", *mypy_args],
            cwd=str(repo_root / cwd_rel),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            for raw_line in (proc.stdout + "\n" + proc.stderr).splitlines():
                if ": error:" in raw_line:
                    parts = raw_line.split(":", 3)
                    fpath = f"{cwd_rel}/{parts[0]}" if len(parts) >= 1 else cwd_rel
                    lineno = int(parts[1]) if len(parts) >= 2 and parts[1].strip().isdigit() else 1
                    msg = parts[3].strip() if len(parts) >= 4 else raw_line
                    dedup_key = (fpath, lineno, msg)
                    if dedup_key in seen:
                        continue
                    seen.add(dedup_key)
                    findings.append(
                        Finding(
                            check="MYPY_TYPE_CHECK",
                            severity="MEDIUM",
                            invariant="INV-TYPES-1",
                            rel_path=fpath,
                            line=lineno,
                            symbol="mypy",
                            message=msg,
                        )
                    )
    return findings


def run_all_checks(
    repo_root: pathlib.Path,
    with_mypy: bool = False,
    python_exe: str = sys.executable,
) -> list[Finding]:
    all_findings: list[Finding] = []
    all_findings.extend(check_shared_authority_isolation(repo_root))
    all_findings.extend(check_async_await_under_lock(repo_root))
    all_findings.extend(check_lock_hierarchy_and_guards(repo_root))
    all_findings.extend(check_k8s_timeout_and_client(repo_root))
    all_findings.extend(check_base_exception_rollback(repo_root))
    all_findings.extend(check_sync_async_ast_parity(repo_root))
    all_findings.extend(check_pep561_exports(repo_root))
    if with_mypy:
        all_findings.extend(run_mypy_checks(repo_root, python_exe))
    return all_findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = pathlib.Path(__file__).resolve().parents[2]
    parser.add_argument("--repo-root", type=pathlib.Path, default=default_root)
    parser.add_argument("--json", action="store_true", help="Emit findings as JSON")
    parser.add_argument(
        "--with-mypy", action="store_true", help="Also run mypy on SDK and agent-sandbox-rl"
    )
    parser.add_argument(
        "--fail-on",
        choices=["CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"],
        default="MEDIUM",
        help="Minimum severity that triggers non-zero exit code (default: MEDIUM)",
    )
    args = parser.parse_args()

    findings = run_all_checks(args.repo_root.resolve(), with_mypy=args.with_mypy)
    rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "NONE": 99}

    if args.json:
        print(json.dumps([dataclasses.asdict(f) for f in findings], indent=2))
    else:
        print("=" * 108)
        print(f"BATCH STATIC AST & CALL-GRAPH AUDITOR (root: {args.repo_root.resolve()})")
        print("=" * 108)
        if not findings:
            print("PASS: 0 static violations found.")
        else:
            for idx, f in enumerate(findings, 1):
                print(
                    f"[{idx:02d}] [{f.severity:<8}] {f.check} ({f.invariant})\n"
                    f"     Location: {f.rel_path}:{f.line} in {f.symbol}\n"
                    f"     Detail:   {f.message}"
                )
            print("-" * 108)
            counts = {
                sev: sum(1 for f in findings if f.severity == sev)
                for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW")
            }
            print(
                f"Summary: {len(findings)} finding(s) "
                f"(CRITICAL={counts['CRITICAL']}, HIGH={counts['HIGH']}, "
                f"MEDIUM={counts['MEDIUM']}, LOW={counts['LOW']})"
            )

    threshold = rank[args.fail_on]
    blocking = [f for f in findings if rank.get(f.severity, 0) >= threshold]
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
