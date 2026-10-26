# G3 memleak fix 2: registry hook, cycle fix, exit hook options

Author: G task conversation, 2026-10-26. Written on upstream/main `f83f2d87`; at push time (21:13) both PRs were rebased onto the new upstream/main `96ef188e`, which changes nothing under `clients/python` (the SDK trees before and after the rebase are identical: PR 1 `823b3692`, PR 2 `0cef7afd`). Changed tests and mypy were rerun after the rebase and pass. Measurements ran on the pre-rebase commits, which have the same SDK trees. Paths are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/` unless stated otherwise. MB means MiB. Facts are marked measured, verified (read in code at `f83f2d87`) or inferred.

## Summary

- **PR 1 (registry)**, `fix/python-sdk-release-terminated-handles` at `c2c006ee` (measured as `4c792840`, same SDK tree): `terminate()` calls a client hook after its delete succeeds, and the hook removes the handle's entry only if it is still that same handle. Both shells. The lazy prune is replaced. Three contracts, each with an owning test in both shells; all five mutations fail as intended.
- **PR 2 (cycle)**, `fix/python-sdk-free-closed-connectors` at `b3574fa9` (measured as `7af54d92`, same SDK tree): one connector method, `_release_sandbox_callbacks()`, clears the handle's callbacks on the connector and its strategy. The async connector calls it at the end of its final `close()`; the sync handle calls it from `close_connection()`. Every cleared reference is mutation-checked separately.
- **PR 3 (exit hook)**: facts verified and the per-client cost measured (each closed async client keeps about 22 KiB alive). Options are below. Not implemented, waiting for Brian.
- **Measured on `par-mem`**, 600 rollouts per arm:
  - RSS slope per 100 rollouts: upstream main 90.1 MB, PR 1 29.3 MB, PR 1 + PR 2 0.46 MB (0.6% of main).
  - Brian's case (create 100, terminate all, create nothing more), retained per handle after 60 s: main 0.667 MB, PR 1 0.660 MB, PR 1 + PR 2 0.0001 MB.
  - All success criteria are met.
- **Pushed** at 21:13: PR 1 (force-with-lease over `2e02e420`), PR 2 (new branch), and the bench branch.

## Step 0

The pending `bench/async-mem-probe` work was pushed at 19:11 (`2ad745c0..c22e0686`): the three earlier commits, the 2,400-rollout results and `handle_cycle_edges.py`. Two more commits followed and were pushed at 21:13: `97b80123` (harness and `client_retained.py`) and `fe4b07a0` (G3 results).

## PR 1. `terminate()` removes its own entry

Diff: 5 files, +80. Each handle class has a class attribute `_on_terminated: Callable[["Sandbox"], None] | None = None` (same for `AsyncSandbox`), and `terminate()` calls it after `delete_sandbox_claim` and before `claim_name = None` (`sandbox.py:228-234`, `async_sandbox.py:236-241`). The clients set `handle._on_terminated = self._untrack` next to both inserts (`create_sandbox` and `get_sandbox`). `_untrack` removes the entry only if `self._active_connection_sandboxes.get(key) is sandbox`.

Choices:
- **The hook takes the base handle type, not `Self`.** mypy types an unannotated `self` as the class, so `Callable[[Self], None]` failed (verified, run). `_untrack` accepts the base type, so no type is widened.
- **`_untrack` has a `claim_name is None` check.** It only narrows the type, because `terminate()` returns early when `claim_name` is empty (`sandbox.py:219-221`). The comment says so.
- **A private attribute, not a constructor kwarg.** Third-party subclasses with a fixed `__init__` keep working (the audit's reason).
- **The `delete_sandbox` pop stays** (`sandbox_client.py:386`, `async_sandbox_client.py:514`). It keeps today's behavior for subclasses whose `terminate()` skips `super()`, and the mocked test `test_sandboxclient.py:185-192` relies on it.
- **No new lock.** The sync client locks nothing today, and the `is`-guarded removal is safer than the unconditional pop. The async hook has no `await` between its check and the delete.

Tests: in `test_adoption_lifecycle.py`, which runs the real clients and handles against an in-memory Kubernetes API, parametrized over both shells.
- `test_terminate_stops_tracking_the_handle`, parametrized over handles from `create_sandbox` and from `get_sandbox`.
- `test_failed_terminate_stays_tracked_for_exit_cleanup`. It also runs the exit hook and checks the claim is deleted.
- `test_terminating_a_replaced_handle_keeps_its_replacement`.
- `close_connection()` keeping the entry is covered by the existing `test_failed_lookup_never_deletes_claim` (`test_adoption_lifecycle.py:542-562`), so no test was added for it.

Mutation checks (measured), each failing exactly the intended test in both shells:

| mutation | failing test |
| --- | --- |
| hook call removed | terminate stops tracking (4 cases) |
| hook moved before the delete | failed terminate stays tracked |
| `is` check dropped | replaced handle keeps its replacement |
| `get_sandbox` wiring removed | terminate stops tracking [get_sandbox] |
| `create_sandbox` wiring removed | terminate stops tracking [create_sandbox] |

The whole file passes (111 tests), mypy is clean, pycodestyle reports nothing on changed lines, and `merge-tree` against upstream/main is clean. No docstrings changed. The docs target renders only public, docstringed members of `sandbox_client` and `models`.

## PR 2. Free a closed handle without the cyclic GC

Diff: 5 files, +109 (sync `connector.py` +19, `async_connector.py` +14, `sandbox.py` +2, tests +74).

Edges cleared:
- sync connector `_get_pod_ip`, `_get_pod_name` and `_get_service_fqdn` (`connector.py:647-649`);
- sync `SandboxdPodTunnelStrategy._get_pod_name` (`:401`), `SandboxdInClusterStrategy._get_pod_ip` / `_get_service_fqdn` (`:532-533`) and `InClusterConnectionStrategy._get_pod_ip` (`:596`);
- async connector `_get_pod_ip` / `_get_service_fqdn` (`async_connector.py:149-150`);
- async pod tunnel `_get_pod_name` (`:664`) and async in-cluster `_get_pod_ip` / `_get_service_fqdn` (`:843-844`).

All of these were already `Optional`, so no type is widened. The strategy references themselves are kept: `close()`, a retried close, `_close_for_atexit` and `is_sandboxd()` still use them.

Choices:
- **Async calls the method at the end of the outermost `finally` in `close()`.** It runs even if a step raises, and runs last, so an in-flight connect sees the callbacks until the close is done. A retried close works because the method is idempotent.
- **Sync calls it from `Sandbox.close_connection()` right after `self.connector.close()`.** The sync connector's `close()` is also a reset (request errors, port-forward restarts, the snapshot extension at `sandbox_with_snapshot_support.py:279, :330`). If `connector.close()` raises, the release is skipped, which is fine because `_is_closed` stays False.
- **The sync release holds `_transport_lock`.** This came from the reviewer: the strategies check a callback and then call it under that lock (`connector.py:602/605`, `:439`, `:543`, `:552`), so an unlocked release could raise `TypeError` on another thread.
- **Dead store.** The async connector's `_get_service_fqdn` is assigned at `:150` and never read (verified). It still holds a bound method, so it is part of the cycle: mutation-checked, leaving it set keeps all four modes leaking. It is cleared rather than silently removed. Removing the dead store instead is a decision for Brian.

Tests:
- One test per shell, `test_closed_handle_is_freed_without_the_cycle_collector`, parametrized over Direct, InCluster, SandboxdPodTunnel and SandboxdInCluster. It builds the handle with a mock `k8s_helper`, runs `gc.disable()` in try/finally, closes, deletes, and asserts that weakrefs to the handle and its connector are dead.
- A sync-only test for the lock: release waits while another thread holds `_transport_lock`.

Mutation checks (measured):
- The call site removed (sync `sandbox.py`, async `close()`): all 4 modes fail.
- Each of the 12 assignments removed separately: the connector-level ones fail all 4 modes, and each strategy-level one fails exactly its mode.
- The lock removed: the lock test fails.
- Reset paths are untouched: `test_connector.py` (reconnects, port-forward restarts), the whole snapshot extension suite (84 passed) and the handle suites all pass. mypy is clean, lint is clean on changed lines, and `merge-tree` is clean.

## PR 3. The exit hook keeps closed clients alive (options, not implemented)

Verified at `f83f2d87`:
- The async client registers `atexit.register(self._atexit_cleanup)` (`async_sandbox_client.py:203`), with `cleanup=True` by default (`:147`).
- Async `close()` keeps handles whose `close_connection()` failed, so a later call can retry them (`:224-238`).
- `AsyncK8sHelper.close()` resets `_api_client` and `_initialized`, so the client can be used again (`async_k8s_helper.py:561-570`). Today the exit hook covers both cases.
- The sync client registers `atexit.register(self._delete_automatic_sandboxes)` (`sandbox_client.py:106`), but only with `cleanup=True`, whose default is False (`:64`). It never unregisters and has no `close()`, only `__exit__` (`:111-118`).

Measured (`results/leakfix/client_retained.py`, driver pod, Python 3.12.15): creating and closing 200 async clients with the default `cleanup=True` leaves 200 of 200 alive after `gc.collect()`, at 11.7 KiB of malloc'd memory plus 9.9 KiB traced by tracemalloc per client (about 21 MiB per 1,000 clients). With `cleanup=False`, 0 of 200 stay alive.

Options (a decision for Brian, not "smallest wins"):
- **(a) Unregister in `close()` when the registry is empty afterwards.**
  - Gain: frees cleanly closed clients.
  - Cost: a client reused after `close()` loses exit cleanup for sandboxes it creates later, unless it re-registers on the next insert. That needs a flag, which is new state.
  - Keeps the hook when a handle failed to close (correct). About 5 lines plus a flag.
- **(b) Weak-reference hook.** A module-level function holding `weakref.ref(client)` is registered instead of the bound method.
  - Gain: a closed or dropped client is freed.
  - Cost: a client dropped without `close()` and with no live handles loses exit cleanup for its remaining claims (they leak in the cluster), and port-forward subprocesses of such handles are not killed at exit.
  - Each client still leaves one small registered function behind (inferred: a few hundred bytes). With PR 1, tracked handles hold the client through `_untrack`, so a client with live handles stays alive anyway (inferred).
- **(c) Leave the code and document it.** Say clients are meant to be long-lived and reused, and that `cleanup=False` plus explicit `delete_all()` suits programs that create many clients.
  - Cost: about 22 KiB per closed client stays until exit.
- **Sync (separate option, not implemented): unregister in `__exit__`.**
  - Cost: behavior change for a client reused after its `with` block, whose later sandboxes lose exit cleanup. Only matters with `cleanup=True`.

My view: the measured cost is small, about 22 KiB per client. (c) is honest and free. (a) is the cleanest code fix if Brian wants one. (b) trades a memory leak for a possible claim leak, which is worse for a cluster.

## Measurements (par-mem, 2026-10-26)

Host: 24 CPUs, 94 GiB. kind v0.32.0, Kubernetes v1.36.1. Harness `97b80123`, `bench_dirty` false. Each arm started at 120 Ready, with 0 errors. SDK installed per commit with `setup-venvs.sh sdk-pod`.

| arm | SDK commit (tree) | version |
| --- | --- | --- |
| main | `f83f2d87` (tree `55d481c8`) | `0.0.0+sdk.f83f2d8` |
| PR 1 | `4c792840` (tree `4a6fae1b`) | `0.0.0+sdk.4c79284` |
| PR 1 + PR 2 | local merge `2e637e61` of `4c792840` and `7af54d92` (PR 2 tree `ce81a49f`), merge tree `93981059`, kept as local branch `plan/g3-measure-pr1-pr2` | `0.0.0+sdk.2e637e6` |

Rolling, 600 rollouts (C=40, hold 10 s):

| arm | rollouts/min | warm min | RSS first/last MB | 2nd-half slope MB/100 rollouts | ratio | uordblks first/last/max MB |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| main | 135.7 | 0 | 156 / 525 | 90.1 | 1.00 | 72 / 436 / 437 |
| PR 1 | 157.3 | 0 | 156 / 404 | 29.3 | 0.38 | 73 / 278 / 309 |
| PR 1 + PR 2 | 168.4 | 1 | 156 / 163 | 0.46 | 0.006 | 72 / 41 / 72 |

Success criteria met: the slope ratio is 0.006 (target at most 0.10), and `uordblks` ends 32 MB below its first in-window sample (target within 50).

Brian's case, scaled down to 100 handles: one warm-up, then create 100, use each once, terminate all, keep no references, and sample for 60 s.

| arm | uordblks before / after create / after terminate / +60 s MB | tracked after | retained MB per handle |
| --- | --- | ---: | ---: |
| main | 33.3 / 109.0 / 111.9 / 100.1 | 101 | 0.667 |
| PR 1 | 33.4 / 107.7 / 111.3 / 99.4 | 0 | 0.660 |
| PR 1 + PR 2 | 32.8 / 108.4 / 44.6 / 32.8 | 0 | 0.0001 |

Success criterion met: PR 1 + PR 2 retains 0.02% of main. PR 1 alone empties the registry but frees nothing, because no allocation triggers the cyclic GC; that is the evidence for issue B. RSS is about 200 MB at the end in all arms, since glibc keeps the freed heap.

PR 3 client micro-measurement (main only, since no fix is chosen yet): 200 of 200 closed clients stay alive, at 11.7 KiB malloc plus 9.9 KiB traced each, with `cleanup=True`.

Plot: `test/benchmarks/batch-claim/results/g3/g3.png` on `bench/async-mem-probe`, with numbers in `summary.json` and setup in `README.md` (pushed once Brian is back).

## Self-check

- **PR 1 reviewer** (fresh, given base, head, problem and rules): approach sound, parity exact, 744 tests and mypy pass, mutants killed. Findings and how they were resolved:
  1. The `get_sandbox` wiring was untested; removing it passed the whole suite. Fixed: the owning test is parametrized over `create_sandbox` and `get_sandbox`, and the mutation is now killed.
  2. The handle now references the client through the hook, so a handle kept after terminate keeps its client alive. Not changed, and listed as a decision.
  3. Behavior change: `client.delete_sandbox(name)` after `sandbox.terminate()` now sends one DELETE (404 is success), where it used to send none. Conversely, exit cleanup no longer re-sends a DELETE per terminated key. Both go in the PR body.
  4. The None check only narrows the type. The comment now says so.
  5. The `delete_sandbox` pop can, in async, remove a replacement created during its await. It stays, per the prompt, and is noted in the PR body.
  6. No lock is needed in async. Noted in the PR body.
- **PR 2 reviewer** (fresh): correct, minimal, all 14 release lines mutation-covered, and every callback edge covered in both shells. Findings and how they were resolved:
  1. Sync use after `close_connection()` changes behavior. In-cluster mode silently falls back to DNS, Gateway mode loses the `X-Sandbox-Pod-IP` header, and the sandboxd modes fail loudly. Verified and run. Flagged in the PR body and as a decision; issue 5 is the proper fix.
  2. The sync release was not under `_transport_lock`. Fixed, with an owning lock test that is mutation-checked.
  3. The sync release is skipped if `connector.close()` raises. Accepted, because `_is_closed` stays False.
  4. Comment wording inside the sync connector. Reworded to "once the handle is closed for good".

## Decisions for Brian

1. **PR 3**: option (a), (b) or (c), and whether to add the sync `__exit__` option.
2. **PR 2's sync use-after-close change** (in-cluster DNS fallback, Gateway header lost): accept and document it in PR 2, or land issue 5's split (a final, refusing sync `close()`) first.
3. **The async connector's dead `_get_service_fqdn` store** (`async_connector.py:150`): keep clearing it in PR 2 (current), or remove the dead store in a separate small change.
4. **PR 1's hook keeps the client alive while a terminated handle is referenced.** Leave it (current) or also clear `self._on_terminated` after the call (one line).
5. **Pushes**: done at 21:13 (see the summary).
6. **Issues to file**: A and B (with PR 1 and PR 2), and 3, 4 and 5 (text only).

`par-mem` is still running (driver pod venvs for `f83f2d8`, `4c79284` and `2e637e6`).

## Texts for Brian

### Issue A

```markdown
Title: Python SDK: the client keeps every terminated sandbox handle for as long as it lives

**What happens**
`SandboxClient` and `AsyncSandboxClient` store every handle they create or attach in `_active_connection_sandboxes`. `Sandbox.terminate()` and `AsyncSandbox.terminate()` close the connection and delete the claim, but leave the handle in that dict. A long-running program that creates and terminates sandboxes therefore keeps one handle per sandbox it ever terminated, until the client is closed. Each async handle holds its closed connector, about 0.7 MiB, because the closed `httpx.AsyncClient` keeps its SSL context and CA bundle. The sync handle holds about 0.06 MiB.

**Measured** (kind, Python 3.12.15, glibc 2.41, 40 workers doing create / one command / terminate)
- 2,400 cycles on main (840fdfde): malloc'd memory in use grew 73 → 469 → 865 → 1,267 → 1,662 MiB at the quartiles, and RSS went from 156 to 1,757 MiB, still rising.
- 600 cycles on main (f83f2d87): RSS grew 90 MiB per 100 cycles in the second half.

**Workarounds**
- Call `client.delete_sandbox(claim_name, namespace)` instead of `sandbox.terminate()`; it removes the entry.
- Or call `client.list_active_sandboxes()` now and then; it drops inactive handles. Note that it also drops closed handles whose sandbox still exists (see the related issue on `list_active_sandboxes`).

**Related**
- The async client's exit cleanup sends one serial DELETE per tracked key, so every terminated handle still tracked costs one redundant DELETE (a 404, treated as success) at exit. This is inferred from the code (`_atexit_cleanup` loops over the keys) and was not measured.
- `_explicit_claims` is also never pruned. It is tiny (one tuple of strings per explicit claim name) and out of scope here.
- #<issue B> is a separate defect: even once the client drops the handle, the handle is freed only by the cyclic garbage collector.

This issue is what causes unbounded growth. #<issue B> causes late cleanup with high peaks.
```

### Issue B

```markdown
Title: Python SDK: a closed sandbox handle is freed only by the cyclic garbage collector

**What happens**
A sandbox handle passes its bound methods (`get_pod_ip`, `get_pod_name`, `get_service_fqdn`) to its connector, which passes them on to its connection strategy. Handle and connector form a reference cycle, so a closed handle that nobody references is not freed by reference counting. Only the cyclic garbage collector frees it. That collector triggers on object counts, not bytes, and cannot see the roughly 0.7 MiB of OpenSSL state behind each closed async handle. Memory therefore climbs to a high, GC-timed peak before it falls, and if the program stops allocating, it stays there.

This affects closed handles whether or not the client still tracks them.

**Measured** (kind, Python 3.12.15, async client, with the registry fix from #<issue A> applied so the client no longer holds terminated handles)
- Rolling, 600 create / command / terminate cycles with 40 workers: RSS grew 29.3 MiB per 100 cycles in the second half, and malloc'd memory peaked at 309 MiB. With the cycle broken: 0.46 MiB per 100 cycles, and memory ended below where it started.
- Create 100 sandboxes, terminate all, create nothing more: 60 s later, 0.66 MiB per handle is still allocated with the registry empty, because nothing triggers a collection. With the cycle broken: 0.0001 MiB per handle.
- With the GC off, a closed, dropped `AsyncSandbox` holds 0.69 MiB; clearing the callbacks after close leaves 0.006 MiB.

**Not covered by a fix to this issue**
Handles dropped without being closed keep their live connector and its sessions until the cyclic GC runs. Breaking the cycle at close does not help them. Close or terminate handles you are done with.

**Related**
#<issue A> (the client keeps every terminated handle). That issue causes unbounded growth; this one causes late cleanup with high peaks.
```

### Issue 3

```markdown
Title: Python SDK: list_active_sandboxes() drops handles whose sandbox still exists, so exit cleanup never deletes them

`list_active_sandboxes()` removes every tracked handle that is not active (`sandbox_client.py` and `async_sandbox_client.py`, the loop that pops `not obj.is_active`). A handle becomes inactive when its connection is closed, which happens on `close_connection()`, on a failed `get_sandbox()` lookup ("close locally and stay tracked"), and in `close()`. The claim of such a handle still exists in the cluster.

Once `list_active_sandboxes()` has dropped it, nothing deletes that claim: `delete_all()`, the context managers and the exit hook only walk the tracked handles. A program that closes a handle, lists, and exits therefore leaves the sandbox running. (Verified in the code; not run against a cluster.)

Suggested direction: only drop handles whose claim is gone (after a successful terminate) and still report closed handles, or report only active ones without dropping the rest. Related: #<issue A>.
```

### Issue 4

```markdown
Title: Python SDK: the client's exit hook keeps every client alive until the program exits

`AsyncSandboxClient.__init__` registers `atexit.register(self._atexit_cleanup)` when `cleanup=True`, which is the default. The atexit module then holds a reference to the client for the life of the process, even after `close()`. `SandboxClient` does the same with `atexit.register(self._delete_automatic_sandboxes)`, but only with `cleanup=True`, whose default there is False. Neither client ever unregisters.

Measured (Python 3.12.15): creating and closing 200 `AsyncSandboxClient`s with the defaults leaves all 200 alive after `gc.collect()`, at about 22 KiB each (11.7 KiB malloc'd plus 9.9 KiB of Python objects), or about 21 MiB per 1,000 clients. With `cleanup=False`, none stay alive. A program that creates a client per task grows by that much per task.

Why it isn't a one-line fix: async `close()` is not final. It keeps handles whose close failed so a later call can retry, and the client can be used again after `close()` because the Kubernetes helper re-initializes lazily. Today the exit hook covers both cases.

Options:
- (a) Unregister in `close()` only when no handle is left tracked, and re-register on the next tracked handle. Needs a small flag.
- (b) Register a module-level function holding a weak reference to the client. Cost: a client dropped without `close()` loses exit cleanup for its remaining claims, and its port-forward subprocesses are not killed at exit.
- (c) Document that clients are long-lived and meant to be reused, and that programs creating many clients should pass `cleanup=False` and call `delete_all()` themselves.
- For `SandboxClient`: unregister in `__exit__`. That changes behavior for a client reused after its `with` block.

Related: #<issue A>, #<issue B>.
```

### Issue 5

```markdown
Title: Python SDK: the sync connector's close() is both a reset and a final close

`SandboxConnector.close()` does two jobs under one name:
- a reset that the next request recovers from: `send_request` calls it on port-forward and connection errors, strategies call their own `close()` before restarting a port-forward, and the GKE snapshot extension calls `connector.close()` on a live handle during suspend and resume;
- the final close, from `Sandbox.close_connection()`.

The async connector splits these. `close()` is final and later use raises `RuntimeError("sandbox connector is closed")`, while errors reset inline and the pod tunnel strategy has a separate `_close_locked()` for resets.

History: the shells were different from the start. Sync `close()` was written as a reset in #382, and async `close()` as final in #510. #1438 made the async side explicit (`_closed`, `_ensure_open`) and #1697 touched both without aligning them. The difference comes from the HTTP libraries rather than from sync versus async: `requests.Session` keeps working after `close()`, while `httpx` refuses requests after `close()` in both its sync and async clients. Python's `io`, httpx, aiohttp and gRPC all treat `close()` as final and idempotent, and keep reconnects as an internal step.

A practical effect: anything that keeps using a sync connector, executor or filesystem after `close_connection()` gets mode-dependent behavior instead of an error. The fix for #<issue B> makes this more visible: after it, in-cluster mode falls back to DNS and Gateway mode stops sending `X-Sandbox-Pod-IP`, while the sandboxd modes fail.

Proposal (a separate change): split a private reset from a final `close()` in the sync connector and its strategies, have the final close refuse later use like the async one, and switch the snapshot extension to the reset.

Related: #1785, which counts ten sync/async divergences and names five; this split may be one of the unnamed ones.
```

### PR 1

```markdown
Title: fix(python-sdk): drop terminated sandboxes from the client (#<issue A>)

Fixes #<issue A>

**Problem.** `SandboxClient` and `AsyncSandboxClient` keep every handle they create or attach in `_active_connection_sandboxes`, and `terminate()` leaves it there. Each kept async handle holds about 0.7 MiB, so a driver that creates and terminates sandboxes grows without bound: 1,662 MiB of malloc'd memory after 2,400 cycles on main.

**Change.** The client gives each handle it tracks a private `_on_terminated` hook. `terminate()` calls it after the delete succeeds and before it clears `claim_name`, and the client removes the entry only if it is still that same handle. A failed delete keeps the entry, so `delete_all()`, the context managers and exit cleanup still retry it. Closed handles whose claim still exists stay tracked, as before. Both clients.

Notes:
- The pop after `terminate()` in `delete_sandbox()` stays on purpose. It covers subclasses whose `terminate()` doesn't call `super()`.
- The sync client takes no lock, as before. The identity-checked removal is safer than the existing unconditional pop. The async hook has no `await` between its check and its delete.
- Small behavior differences: `client.delete_sandbox(name)` after `sandbox.terminate()` now sends one DELETE that returns 404 (treated as success) instead of none, and exit cleanup no longer sends a DELETE for every terminated handle. A handle now refers to its client through the hook.

**Alternatives considered.**
- Pruning terminated handles on the next insert: frees nothing after the last create.
- A constructor argument: breaks subclasses with a fixed `__init__`.
- Weak references in the registry: would lose cleanup for handles the caller dropped.
- Routing terminate through the client: `sandbox.terminate()` would still leak.

**Tests.** In `test_adoption_lifecycle.py`, against the in-memory API, both clients:
- terminate stops tracking the handle (from `create_sandbox` and from `get_sandbox`);
- a failed terminate stays tracked and exit cleanup deletes the claim;
- terminating a replaced handle keeps its replacement.
Each is mutation-checked.

**Measured** (kind, 600 create / command / terminate cycles, 40 workers, async): RSS slope 90.1 MiB per 100 cycles on main against 29.3 with this change. The rest of the growth is #<issue B>, fixed separately.
```

### PR 2

```markdown
Title: fix(python-sdk): free closed sandbox handles without the cyclic GC (#<issue B>)

Fixes #<issue B>

**Problem.** A handle passes its bound methods to its connector and strategy, so handle and connector form a reference cycle. A closed handle that nobody references is freed only by the cyclic garbage collector, which can't see the ~0.7 MiB of OpenSSL state each closed async handle holds. With #<issue A> fixed, 600 cycles still grow RSS by 29.3 MiB per 100 cycles. After creating and terminating 100 sandboxes, 0.66 MiB per handle stays allocated as long as nothing else allocates.

**Change.** Each connector gets `_release_sandbox_callbacks()`, which clears the stored callbacks on the connector and its strategy.
- The async connector calls it at the end of its final `close()`.
- The sync handle calls it from `close_connection()`, right after `connector.close()`. The sync connector's `close()` also serves as a reset for reconnects and for snapshot suspend and resume, which must keep the callbacks.
- The sync release holds the transport lock, so a connect on another thread never sees a callback cleared between checking and calling it.

The fields were already optional. The async connector's `_get_service_fqdn` is never read but still holds a bound method, so it is cleared too.

Behavior note: code that keeps using a sync connector, executor or filesystem after `close_connection()` now gets DNS instead of the pod IP in in-cluster mode, and no `X-Sandbox-Pod-IP` header in Gateway mode. The sandboxd modes fail as before. #<issue 5> proposes making a closed sync connector refuse use, as the async one does.

**Alternatives considered.**
- Leave it to the GC: peak set by GC timing, and nothing is freed if nothing else allocates.
- Call `gc.collect()` from the library: global pauses.
- Weak method references: a handle that only an executor still references would lose its callbacks silently.
- Clear inside the sync connector's `close()`: breaks reconnects and snapshot resume.

**Tests.**
- Per client, with the GC disabled: a closed, dropped handle and its connector are freed. Parametrized over Direct, InCluster, SandboxdPodTunnel and SandboxdInCluster. Each cleared reference is mutation-checked separately.
- A sync test that the release waits for the transport lock.
- The reconnect and snapshot extension tests pass unchanged.

**Measured** (kind, async, on top of #<PR 1>):
- 600 cycles: RSS slope 0.46 MiB per 100 cycles against 29.3 without this change and 90.1 on main.
- Create 100, terminate all: 0.0001 MiB per handle retained against 0.66.
```

### PR 3 (draft, pending the option choice)

```markdown
Title: fix(python-sdk): stop the exit hook from keeping closed clients alive (#<issue 4>)

Fixes #<issue 4>

**Problem.** `AsyncSandboxClient` registers `atexit.register(self._atexit_cleanup)` by default, so every client stays alive until exit, even after `close()`: about 22 KiB per client, or 200 of 200 closed clients alive after `gc.collect()`.

**Change.** <the chosen option: (a) unregister in close() when no handle is left tracked, re-registering on the next tracked handle; or (b) a weak-reference exit hook; or (c) documentation only>

**Alternatives considered.** <the two options not chosen, one line each, with their costs from #<issue 4>>

**Tests.** Patch `atexit.register` / `atexit.unregister` in the client module and assert registration and unregistration around `close()` and reuse. Unregister in `finally` so no hook leaks into pytest. `gc.collect()` is used only to check that a closed client is freed.

**Measured.** 200 clients created and closed, memory and live count per client, before and after.
```

## Links

- PR 1: https://github.com/briankhoi/agent-sandbox/compare/96ef188e..fix/python-sdk-release-terminated-handles
- PR 2: https://github.com/briankhoi/agent-sandbox/compare/96ef188e..fix/python-sdk-free-closed-connectors
- PR 3: not created yet (no commits until an option is chosen).
- Bench: https://github.com/briankhoi/agent-sandbox/compare/c22e0686..bench/async-mem-probe
- Plot: https://github.com/briankhoi/agent-sandbox/blob/bench/async-mem-probe/test/benchmarks/batch-claim/results/g3/g3.png
