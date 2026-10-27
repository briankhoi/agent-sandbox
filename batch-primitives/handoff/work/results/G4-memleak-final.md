# G4 memleak final: registry-bound hook, exit hook, combined verification

Author: G task conversation, 2026-10-27. Base upstream/main `7f40dc1a` (fetched 00:07, unchanged since 23:32). Paths are under `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`. MB means MiB. Facts are marked measured, verified (read) or inferred.

## Summary

- **PR 1** `0a54a028` (`fix/python-sdk-release-terminated-handles`): `_untrack` is now a staticmethod `(registry, sandbox)`, and each handle's `_on_terminated` is `functools.partial(self._untrack, self._active_connection_sandboxes)`. A tracked handle no longer keeps its client alive. Behavior is unchanged.
- **PR 3** `53765391` (`fix/python-sdk-release-client-exit-hook`, stacked on PR 1): option (e) in both clients. The exit cleanup methods are static and registered bound to the registry, the explicit-claims set and (async) the injected ApiClient. Every closed client is freed, and exit behavior is unchanged.
- **PR 2** `b3574fa9`: unchanged code. Only its description gains the use-after-close sentence.
- **Combined** (local merge `4a9af230`, tree `2cf0882d`), all measured: the 25 PR-added tests pass together, and 580 tests pass across the adoption, client, handle, connector, lifecycle-integration and snapshot-extension files.
- **Memory**, merge against main:
  - rolling RSS slope 0.85 vs 71.6 MB per 100 rollouts (1.1%);
  - retained per handle after create 100 / terminate all 0.0003 vs 0.667 MB (0.04%);
  - closed clients: 0 of 200 alive vs 200 of 200, with about 610 B left per client vs 1.4 to 11.4 KB.
  
  All criteria are met except the about-500 B client target: 610 B on Python 3.12, against the audit's 480 B on Python 3.13.
- **Pushed**:
  - PR 1, force-with-lease over `f498987f`, an earlier push of this same change;
  - `review-diff/leakfix-pr1` `cdb5e933`;
  - PR 3 (new branch);
  - bench `f8df9206`.

## PR 1. Hook bound to the registry (amended single commit)

Diff since the last review (`review-diff/leakfix-pr1`): 4 files, +62 −10.
- `_untrack` becomes `@staticmethod _untrack(registry, sandbox)` in both clients (`sandbox_client.py:374-386`, `async_sandbox_client.py:487-499`).
- The four places that set the hook use `functools.partial(self._untrack, self._active_connection_sandboxes)` (`sandbox_client.py:290, 354`, `async_sandbox_client.py:376, 473`). Both clients import `functools`.
- The comment says why it is static, and that an instance method would bring the pin back.

Choice: a staticmethod plus `partial`, not a module-level function. Both build the same reference graph; the staticmethod keeps PR 1's name and location, so the diff is smallest (the audit's recommended form). PR 3 now uses the same pattern.

Test (owning, both shells): `test_tracked_handle_does_not_keep_its_client_alive` in `test_sandboxclient.py` and `test_async_sandboxclient.py`. A client with `cleanup=False` and a tracked handle the caller keeps: after `del client`, a weakref to the client is dead with `gc.disable()` (so only reference counting counts).

Mutations (measured):

| mutation | result |
| --- | --- |
| hook restored to capture the client (`lambda sb, _self=self: _self._untrack(...)`) | both new tests fail; the three behavior tests still pass, so behavior is unchanged |
| hook call removed | terminate stops tracking (4 cases) fails |
| hook moved before the delete | failed terminate stays tracked fails |
| `is` check dropped | replaced handle keeps its replacement fails |
| `get_sandbox` wiring removed | terminate stops tracking [get_sandbox] fails |
| `create_sandbox` wiring removed | terminate stops tracking [create_sandbox] fails |

Rebased onto `7f40dc1a`. mypy is clean, and 291 tests pass in the changed files. The commit message changed only where it describes the hook.

## PR 2. Description only

The sentence from the audit is added to the PR 2 text below, with no code change. Issue 5's text is updated with the audit's measured facts.

## PR 3. The exit hook holds only the data it needs

The rule: exit cleanup reads only the registry data (the registry dict, the explicit-claims set and, async only, the injected ApiClient), never the client.

Paths that touch the registry or the exit hook (verified):
- create_sandbox: insert, plus the PR 1 hook;
- get_sandbox: insert, plus the pop of an inactive handle;
- adoption: through create_sandbox with `adopt_existing`;
- terminate: the PR 1 hook removes the entry;
- async `close()`: pops cleanly closed handles;
- `list_active_sandboxes()`: pops inactive handles;
- `delete_sandbox()`, `delete_all()`;
- sync `__exit__` and async `__aexit__`;
- the atexit hook.

None of them rebinds the registry or the set (verified by grep of every assignment in both modules).

Diff (on PR 1): 4 files, +164 −73 (production +48 −22).
- Async: `_atexit_cleanup` becomes `@staticmethod _atexit_cleanup(registry, explicit_claims, injected_api_client)`. Its body only swaps `self.X` for the arguments, so log strings, stderr guards, the temp-dir handling and the timeout are identical.
- Async registration: `atexit.register(functools.partial(self._atexit_cleanup, self._active_connection_sandboxes, self._explicit_claims, api_client))`.
- The now-unused `self._injected_api_client` attribute and its comment are removed. It was assigned only in `__init__` and read only by the hook (verified).
- Sync: `_delete_automatic_sandboxes` becomes static `(registry, explicit_claims)` and is registered the same way; `__exit__` calls it with the client's two containers.
- Sync: `delete_sandbox`'s tracked branch (terminate, pop, the same log) moves to a static `_delete_tracked_sandbox(registry, key, sandbox)`, which both `delete_sandbox` and exit cleanup call, so the rule lives in one place. It still calls `sandbox.terminate()`.
- Both registry assignments carry: "The exit hook holds these two containers, so they are changed in place and never rebound."

Choices:
- **`atexit.register(functools.partial(...))` over `atexit.register(fn, *args)`.** The existing `register.call_args.args[0]()` calls in `test_adoption_lifecycle.py` keep working unedited, and it matches PR 1's partial.
- **Staticmethods, not module functions.** The first version moved the 70-line async body to module level. The reviewer pointed out that staticmethods give the same behavior with a far smaller diff and match PR 1, so I switched. Measured with the earlier version too; its results are kept in `superseded/`.

Tests changed (all in `test_async_sandboxclient.py`, none in production code, none rebinding the registry):
- `test_cleanup_default_registers_atexit`, `test_cleanup_true_registers_atexit`: they asserted the bound method. They now assert the registered partial: its function is `AsyncSandboxClient._atexit_cleanup`, and its args are the client's own registry, its set and the given ApiClient (identity).
- Five injected-ApiClient tests (`..._does_not_fall_back_to_default_kubeconfig_with_injected_api_client`, `..._with_injected_api_client_needs_no_temp_dir`, `..._reflects_injected_client_state_at_cleanup_time_not_construction_time`, `..._does_not_copy_async_refresh_api_key_hook`, `..._sends_injected_bearer_token_on_older_sync_clients`): they rebound `client._active_connection_sandboxes` and called the removed method. They now take the hook the client registered (`_exit_hook_for`, with `atexit` patched), fill the existing dict and call that hook, so the client-to-hook wiring stays covered.
- Six cleanup-body tests (`..._deletes_tracked_claims`, `..._uses_loop_independent_handle_cleanup`, `..._skips_when_no_sandboxes`, `..._suppresses_errors`, `..._suppresses_helper_construction_errors`, `..._suppresses_claim_snapshot_errors`): they now call `AsyncSandboxClient._atexit_cleanup(registry, set(), None)` with their own dict.
- Unchanged and passing: `test_adoption_lifecycle.py` (all four `register.call_args.args[0]()` sites) and the two real-process-exit tests.

Owning tests (new, both shells): `test_dropped_client_is_freed_and_its_exit_hook_still_cleans_up`, parametrized `[registry-empty]` and `[registry-tracking]`.
- Setup: a client with cleanup on (`atexit` patched), a generated and an explicit sandbox.
- Empty case: async closes the client, sync terminates both (sync has no `close()`).
- Then `del client` with `gc.disable()`, so only reference counting counts: the weakref is dead in both cases.
- Then the registered hook is called. With tracking, it deletes only the generated claim, and both handles end up closed. With an empty registry, it deletes nothing.

Mutations (measured, both shells):

| mutation | failing tests |
| --- | --- |
| the hook captures the client | both params of the dropped-client test in both shells, plus the two registration tests |
| the hook gets a copy of the registry | `[registry-tracking]` in both shells, plus the registration tests |
| explicit claims ignored at exit | `[registry-tracking]` in both shells, plus the registration tests |
| sync `__exit__` skips cleanup | `test_context_manager_cleans_up_on_exit` and `..._on_exception` |

382 tests pass in the changed files. mypy is clean, lint is clean on changed lines, no em dash was added, and `merge-tree` against `7f40dc1a` is clean.

## Combined verification (merge `4a9af230` = PR 1 → PR 3 → PR 2)

PR 2 was checked with `merge-tree` against fresh main first (clean). The merge is kept as local branch `plan/g4-verify-all`. Measured:
- 25 PR-added tests pass together: PR 1's 10 (8 adoption-lifecycle cases plus 2 client-pin tests), PR 2's 9 (8 GC-off cases plus the lock test) and PR 3's 6 (4 dropped-client cases plus 2 registration tests).
- 580 tests pass, plus 8 subtests, across `test_adoption_lifecycle.py`, `test_sandboxclient.py`, `test_async_sandboxclient.py`, `test_sandbox.py`, `test_async_sandbox.py`, `test_connector.py`, `test_async_connector.py`, `test_lifecycle_integration.py` and the snapshot-extension tests.
- mypy is clean.

## Memory (par-mem, 2026-10-26, back to back)

Each arm started at 120 Ready with 0 errors, `bench_dirty` false. SDK installed per commit (`/opt/venv-<sha7>`). Main arms ran with harness `6e85baa0`, merge arms with `ac78e0e0`.

| arm | SDK | rolling MB per 100 rollouts (2nd half) | ratio | uordblks first / last MB | burst retained MB per handle | tracked after burst |
| --- | --- | ---: | ---: | --- | ---: | ---: |
| main | `7f40dc1a` (`0.0.0+sdk.7f40dc1`) | 71.6 | 1.00 | 72 / 437 | 0.667 | 101 |
| all three | `4a9af230` (`0.0.0+sdk.4a9af23`) | 0.85 | 0.011 | 72 / 40 | 0.0003 | 0 |

Clients created and closed, 200 per case. The GC is on, `gc.collect()` runs before each reading, and memory is what tracemalloc traces:

| case | main alive / B | merge alive / B |
| --- | --- | --- |
| async default, never used | 200 / 1,369 | 0 / 609 |
| async default, used then closed | 200 / 9,600 | 0 / 621 |
| sync `cleanup=True`, `with` block | 200 / 11,407 | 0 / 606 |
| async / sync `cleanup=False` | 0 / 89, 0 / 90 | 0 / 91, 0 / 81 |

Plot: `test/benchmarks/batch-claim/results/g4/g4.png`. Details are in `results/g4/README.md`. The earlier merge's runs are in `results/g4/superseded/`.

## Self-check

- **PR 1 change** (fresh reviewer; base, head, rules): correct and minimal, with 757 tests passing and the client-capturing mutation killed. Nits:
  1. The new tests are module-level functions in unittest files. Left as is: pytest runs them, and PR 2's tests use the same form.
  2. Sync type style. Fixed with `Dict[Tuple[...]]`, amended into `0a54a028`.
  3. `gc.enable()` runs in `finally` unconditionally. Left as is.
  4. Mixed `T` and base type in the signature. Left as is; not a widening.
  5. The comment's second clause adds little. Kept, because it warns future editors.
  
  It also confirmed that nothing else on a handle references the client.
- **PR 3** (fresh reviewer; rule and paths, no reasoning): correct; exit behavior identical in every case, 761 tests passing, every mutation killed. Findings:
  1. Smaller as staticmethods. Adopted (`53765391`).
  2. Repeated "why" comments. Trimmed to one comment per place.
  3. The registration identity asserts overlap the behavior test. Kept as a like-for-like rewrite of the old tests.
  4. Sync exit and `__exit__` no longer dispatch through an overridable `self.delete_sandbox`. Inferred; no in-repo subclass overrides it. Noted in the PR 3 text.
- **Combined merge** (fresh reviewer, on `8735bb79`, which differs from `4a9af230` only in PR 3's form): the PRs interact as intended, nothing pins a client any more, and 770 tests pass. Findings:
  1. With `cleanup=False`, a client dropped while it tracks a closed handle leaves that handle in a handle → `_on_terminated` partial → registry → handle cycle. That handle is then freed only by the cyclic GC. Measured by the reviewer; memory only, and the sessions are already closed. No minimal fix (a plain dict can't be weakly referenced), so accepted and noted.
  2. Sync subclass dispatch, as for PR 3 above.
  3. PR 3 could drop `_delete_tracked_sandbox` and call `sandbox.terminate()` directly, but the log message would change ("Cleanup failed for" instead of "Failed to delete sandbox"). Rejected, because the brief requires identical log messages.
  4. Two patterns across the stack. Resolved: PR 3 now uses staticmethods like PR 1.
  5. Pre-existing, not introduced:
     - `_explicit_claims` is never pruned. `examples/agent-sandbox-rl/agent_sandbox_rl/handles.py:237-255` still works around it, and its docstring is now half true.
     - PR 2 does not free closed-but-not-terminated GKE snapshot handles, because `SandboxWithSnapshotSupport` keeps a `SnapshotEngine` holding the handle's bound methods (`gke_extensions/snapshots/sandbox_with_snapshot_support.py:87-92`) until `terminate()`.
     
     Both are listed as decisions.

## Decisions for Brian

1. **Client residual: 610 B per client, not about 500.** This is the atexit entry plus an empty dict and set on Python 3.12; the audit measured 480 B on Python 3.13. Accept it, or take the audit's option (f), which adds a module registry and finalizers for zero residual (about 20 lines).
2. **Sync exit cleanup no longer goes through an overridable `delete_sandbox`** (PR 3). Accept and note it (current), or keep `__exit__` calling `self.delete_sandbox`, which would also need the exit hook to differ from `__exit__`.
3. **Follow-ups to file or not**:
   - (a) GKE snapshot handles stay in a cycle after `close_connection()` until `terminate()` (PR 2 does not cover them);
   - (b) `_explicit_claims` is never pruned;
   - (c) the `cleanup=False` dropped client with tracked closed handles leaves them to the cyclic GC (known cost of PR 1's hook).
4. **Filing**: issue 5 now (text below), then open PR 1, PR 2 and PR 3. PR 3 is stacked on PR 1, so open it after PR 1 merges or as a dependent PR.

`par-mem` is still running.

## Texts for Brian

### Issue 4 (updated)

```markdown
Title: Python SDK: the client's exit hook keeps every client alive until the program exits

`AsyncSandboxClient.__init__` registers `atexit.register(self._atexit_cleanup)` when `cleanup=True`, which is its default, and `SandboxClient` registers `atexit.register(self._delete_automatic_sandboxes)` when `cleanup=True` (default False there). A bound method keeps its object, so the atexit list holds each client until the process exits, even after `close()`. Neither client ever unregisters.

Measured (Python 3.12.15, 200 clients each, tracemalloc after `gc.collect()`): every closed client stays alive.

| client | traced memory per closed client |
| --- | ---: |
| async, never used | 1.4 KB |
| async, used then closed | 9.6 KB |
| sync with `cleanup=True` | 11.4 KB |

A program that creates a client per task grows by that much per task. With `cleanup=False` none stay alive.

Why unregistering is not enough: async `close()` is not final. It keeps handles whose close failed so a later call can retry, and the client can be used again after `close()`. Sync `__exit__` closes nothing and the client stays usable. Exit cleanup must keep covering those cases and clients dropped without `close()`.

Proposed fix: register a function bound only to the data exit cleanup reads (the registry dict, the explicit-claims set and, for async, the injected ApiClient) instead of a bound method, in both clients. Exit behavior stays the same in every case, and the client can be freed. About 600 B per client remain (the atexit entry with an empty dict and set). The same pattern is used by urllib3's connection pools (`weakref.finalize(self, _close_pool_connections, pool)`, which passes only the data cleanup needs).

Alternatives:
- Unregistering in `close()` loses exit cleanup for clients reused after `close()`, unless a flag re-registers on every insert path.
- A weak-reference hook loses cleanup for a client dropped while its sandboxes are still in use.
- Unregistering in sync `__exit__` breaks clients reused after the `with` block.

Related: #<issue A>.
```

### Issue 5 (updated)

```markdown
Title: Python SDK: the sync connector's close() is both a reset and a final close

`SandboxConnector.close()` does two jobs under one name:
- a reset the next request recovers from: `send_request` calls it on port-forward and connection errors, strategies call their own `close()` before restarting a port-forward, and the GKE snapshot extension calls `connector.close()` on a live handle during suspend and resume;
- the final close, from `Sandbox.close_connection()`.

The async connector splits these. Its `close()` is final and later use raises `RuntimeError("sandbox connector is closed")`.

History: the shells were different from the start. Sync `close()` was written as a reset in #382 and async `close()` as final in #510. #1438 made the async side explicit, and #1697 touched both without aligning them. The difference comes from the HTTP libraries, not from sync versus async: `requests.Session` keeps working after `close()`, while `httpx` refuses requests after `close()` in both its clients. Python's `io`, httpx, aiohttp and gRPC treat `close()` as final and idempotent, with reconnects as an internal step.

**What a caller sees today.** Code that keeps a sync connection after `close_connection()` or `terminate()`, through `sandbox.connector` or a `commands`/`files` object kept from before the close, gets mode-dependent behavior instead of an error. After #<PR 2>, which drops the handle's callbacks at close (measured on the same handle after `close_connection()`):

| mode | main | after #<PR 2> |
| --- | --- | --- |
| in-cluster | uses the pod IP | falls back to the service DNS name, which works if the Service exists |
| sandboxd pod tunnel | starts a new port-forward | `SandboxPortForwardError: sandbox pod name not resolved yet` |
| sandboxd in-cluster | reconnects | `SandboxNotReadyError` (pod-IP mode) or `SandboxNoServiceError` (service mode) |
| Gateway | sends `X-Sandbox-Pod-IP` | drops only that optional pod-IP hint; the router falls back to its normal lookup |
| direct, local tunnel | works | works (unchanged) |

On main the quiet reconnect after close is itself a leak. It reopens a port-forward or session on a handle the client already closed and stopped tracking, so nothing closes it again (inferred from the code; not measured).

Proposal (a separate change): split a private reset from a final `close()` in the sync connector and its strategies. Have the final close refuse later use with a clear error, as the async one does, and switch the snapshot extension to the reset.

Related: #1785, which counts ten sync/async divergences and names five; this split may be one of the unnamed ones.
```

### PR 1 (updated)

```markdown
Title: fix(python-sdk): drop terminated sandboxes from the client (#<issue A>)

Fixes #<issue A>

**Problem.** `SandboxClient` and `AsyncSandboxClient` keep every handle they create or attach in `_active_connection_sandboxes`, and `terminate()` leaves it there. Each kept async handle holds about 0.7 MiB, so a driver that creates and terminates sandboxes grows without bound: 1,662 MiB of malloc'd memory after 2,400 cycles on main.

**Change.** The client gives each handle it tracks a private `_on_terminated` hook. `terminate()` calls it after the delete succeeds and before it clears `claim_name`. The hook removes the entry only if it is still that same handle. A failed delete keeps the entry, so `delete_all()`, the context managers and exit cleanup still retry it, and closed handles whose claim still exists stay tracked, as before. Both clients.

The hook is the static `_untrack` bound to the registry dict with `functools.partial`, not a method of the client, so a tracked handle never keeps its client alive.

Notes:
- The pop after `terminate()` in `delete_sandbox()` stays on purpose. It covers subclasses whose `terminate()` doesn't call `super()`.
- The sync client takes no lock, as before. The identity-checked removal is safer than the existing unconditional pop. The async hook has no `await` between its check and its delete.
- Small behavior differences: `client.delete_sandbox(name)` after `sandbox.terminate()` now sends one DELETE that returns 404 (treated as success) instead of none, and exit cleanup no longer sends a DELETE for every terminated handle.

**Alternatives considered.**
- Pruning terminated handles on the next insert: frees nothing after the last create.
- A constructor argument: breaks subclasses with a fixed `__init__`.
- A bound method of the client as the hook: pins the client while any handle is tracked.
- Weak references in the registry: loses cleanup for handles the caller dropped.

**Tests.** Against the in-memory API, both clients:
- terminate stops tracking the handle (from `create_sandbox` and from `get_sandbox`);
- a failed terminate stays tracked and exit cleanup deletes the claim;
- terminating a replaced handle keeps its replacement;
- a dropped client with a tracked handle is freed by reference counting.
Each is mutation-checked.

**Measured** (kind, 600 create / command / terminate cycles, 40 workers, async): RSS slope 90.1 MiB per 100 cycles on main against 29.3 with this change. The rest is #<issue B>, fixed separately.
```

### PR 2 (updated, one sentence added)

```markdown
Title: fix(python-sdk): free closed sandbox handles without the cyclic GC (#<issue B>)

Fixes #<issue B>

**Problem.** A handle passes its bound methods to its connector and strategy, so handle and connector form a reference cycle. A closed handle that nobody references is freed only by the cyclic garbage collector, which can't see the ~0.7 MiB of OpenSSL state each closed async handle holds. With #<issue A> fixed, 600 cycles still grow RSS by 29.3 MiB per 100 cycles. After creating and terminating 100 sandboxes, 0.66 MiB per handle stays allocated as long as nothing else allocates.

**Change.** Each connector gets `_release_sandbox_callbacks()`, which clears the stored callbacks on the connector and its strategy.
- The async connector calls it at the end of its final `close()`.
- The sync handle calls it from `close_connection()`, right after `connector.close()`. The sync connector's `close()` also serves as a reset for reconnects and for snapshot suspend and resume, which must keep the callbacks.
- The sync release holds the transport lock, so a connect on another thread never sees a callback cleared between checking and calling it.

The fields were already optional. The async connector's `_get_service_fqdn` is never read but still holds a bound method, so it is cleared too.

Using a sandbox's connection after `close_connection()` or `terminate()` (for example through `sandbox.connector` or a `commands`/`files` object kept from before the close) is unsupported; after this change some connection modes raise or route without the pod IP instead of silently reconnecting, and a follow-up (#<issue 5>) will make the sync client refuse such use with a clear error, as the async client already does.

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

### PR 3

```markdown
Title: fix(python-sdk): stop the exit hook from keeping closed clients alive (#<issue 4>)

Fixes #<issue 4>

Stacked on #<PR 1>: its tests need the registry-bound terminate hook, so a client that still tracks handles can be freed.

**Problem.** `AsyncSandboxClient` registers `atexit.register(self._atexit_cleanup)` by default, and `SandboxClient` registers `self._delete_automatic_sandboxes` with `cleanup=True`. A bound method keeps its object, so every client stays alive until exit, even after `close()`: 1.4 KB per unused and 9.6 KB per used async client, 11.4 KB per sync client (tracemalloc), and 200 of 200 closed clients alive after `gc.collect()`.

**Change.** Exit cleanup reads only the registry, the explicit-claims set and, for async, the injected ApiClient. Both cleanup methods become static and are registered bound to those objects with `functools.partial`, the same pattern as the terminate hook.
- Exit behavior is unchanged: closed, reused, failed-close and never-closed clients are all cleaned up as before.
- The ApiClient object itself is passed, so its settings are still read at exit.
- The registry and the set are never rebound after construction, which the hook relies on (commented at the assignments).
- Sync `__exit__` calls the same method, and `delete_sandbox()` shares the tracked-handle branch with it, so the rule lives in one place. Exit cleanup therefore no longer goes through a subclass's override of sync `delete_sandbox()`; no subclass in this repo overrides it.

About 610 B per client remain (the atexit entry with an empty dict and set).

**Alternatives considered.**
- Unregister in `close()` when nothing is tracked: loses cleanup for clients reused after `close()`, unless a flag re-registers on every insert path.
- A weak-reference hook: loses cleanup for a client dropped while its sandboxes are in use.
- Unregister in sync `__exit__`: breaks reuse after the `with` block.
- Document "reuse one client": leaves the growth.

**Tests.**
- Both clients: a closed and dropped client is freed by reference counting (GC disabled), with an empty registry and with tracked handles. Its registered exit hook still deletes the tracked generated sandbox and only closes the explicit one.
- The async registration tests now check that the hook is bound to the client's registry, set and ApiClient.
- Existing exit-cleanup tests now fill the client's registry, or call the static method with their own, instead of replacing the dict.
- All mutation-checked; the real-process-exit tests pass unchanged.

**Measured** (200 clients created and closed, Python 3.12, GC on, `gc.collect()` before each reading): 0 of 200 alive, about 610 B per client, against 200 of 200 and 1.4 to 11.4 KB on main.
```

## Links

- PR 1 vs its base: https://github.com/briankhoi/agent-sandbox/compare/7f40dc1a..fix/python-sdk-release-terminated-handles
- PR 1 old to new (rebase-only commit, then the hook change): https://github.com/briankhoi/agent-sandbox/compare/c2c006ee..review-diff/leakfix-pr1 (hook change alone: https://github.com/briankhoi/agent-sandbox/compare/9c2c1edb..review-diff/leakfix-pr1)
- PR 3 vs PR 1: https://github.com/briankhoi/agent-sandbox/compare/fix/python-sdk-release-terminated-handles..fix/python-sdk-release-client-exit-hook
- PR 2 (unchanged): https://github.com/briankhoi/agent-sandbox/compare/96ef188e..fix/python-sdk-free-closed-connectors
- Bench: https://github.com/briankhoi/agent-sandbox/compare/fe4b07a0..bench/async-mem-probe
- Plot: https://github.com/briankhoi/agent-sandbox/blob/bench/async-mem-probe/test/benchmarks/batch-claim/results/g4/g4.png
