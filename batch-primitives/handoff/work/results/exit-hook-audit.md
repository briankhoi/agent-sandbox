# Exit hook audit (issue 4 / PR 3)

Independent audit, 2026-10-26, upstream/main `96ef188e`. Read-only on code; scratch in `/tmp/par-audit3`.

## One-minute summary

The leak is real but the cause is narrow: `atexit.register(self.<method>)` stores a bound method, so atexit holds the
whole client. The exit cleanup reads only three objects: the handle registry dict, the explicit-claims set and the
injected `ApiClient` (verified). **Recommended fix (option e, not in the task's list):** register a module-level function
with those three objects as arguments instead of the bound method. Exit behavior stays exactly the same in every case
(closed, reused after close, failed close, dropped without close). There is no new state, no flag and no weakref, and
the client itself is freed. What is left per client is one atexit entry plus an empty dict and set: **480 B measured,
against 1.4 KB (unused) and 8.0 KB (used) traced today**. The task's 22 KiB figure also counts malloc. Weak hooks (b) lose
exit cleanup for a pattern that works today. (a) and (d) need a flag that every insert path has to check. Do the same for
sync in the same PR. Unregistering in sync `__exit__` would be wrong.

## Evidence

- verified `async_sandbox_client.py:202-203`: `if cleanup: atexit.register(self._atexit_cleanup)`, default `cleanup=True` (`:147`).
- verified `:219-233`: `close()` pops only handles whose `close_connection()` succeeded, then `k8s_helper.close()`.
  `async_k8s_helper.py:561-570` resets `_api_client`/`_initialized`, and `_ensure_initialized` (`:68-82`) re-creates it,
  so the client can be reused after `close()`.
- verified `:530-597`: `_atexit_cleanup` reads only `self._active_connection_sandboxes`, `self._explicit_claims` and
  `self._injected_api_client`. It is already synchronous and loop-independent (sync `K8sHelper`, `_close_for_atexit`).
- verified: neither registry attribute is ever reassigned after `__init__` (grep of `self.X =` in both clients), so a
  hook that holds the dict sees every later insert, including after reuse.
- verified `async_sandbox.py:69` and `sandbox.py:61`: handles hold the shared `k8s_helper`, **not the client**. So on main
  `sb = await AsyncSandboxClient(cfg).create_sandbox(...)` drops the client while `sb` is in use. Today the bound-method
  hook keeps that sandbox covered at exit.
- verified `sandbox_client.py:105-118,401-411`: sync registers `self._delete_automatic_sandboxes` (default
  `cleanup=False`, `:64`). `__exit__` runs the same cleanup but closes nothing, so the client stays usable. Explicit
  claims stay tracked after `__exit__` with closed connections.
- inferred (task notes, PR 1 not on main): PR 1 gives handles a hook bound to the client (`_untrack`). Then a tracked
  handle keeps its client alive. That only matters while handles are tracked, and they need exit cleanup anyway.

Measured (`/tmp/par-audit3/measure*.py`, Py 3.13.15, kubernetes 37.0.1, kubernetes_asyncio 36.1.0, kube config mocked,
tracemalloc delta after `gc.collect()`, 1000-2000 clients, warm-up excluded):

| client, create then close | alive | traced B/client |
|---|---|---|
| async `cleanup=False` | 0 | 88 |
| async `cleanup=True` (default), never initialized | all | 1,377 |
| async `cleanup=True`, ApiClient initialized then closed | all | 8,050 |
| sync `with` block, `cleanup=False` / `True` | 0 / all | 91 / 9,371 |
| prototype: data-only hook (option e) | 0 | 480 |

## Prior art (how libraries clean up at exit without pinning objects)

- `weakref.finalize` (https://docs.python.org/3/library/weakref.html#weakref.finalize): a finalizer that is still alive
  runs at exit when `atexit=True`, and also runs **when the object is collected**. The docs warn that func/args must not
  reference obj "otherwise obj will never be garbage collected", which is exactly today's bug.
- urllib3 2.x `HTTPConnectionPool` (https://github.com/urllib3/urllib3/blob/main/src/urllib3/connectionpool.py):
  `weakref.finalize(self, _close_pool_connections, pool)`, with the comment "Do not pass 'self' ... (leak)". It passes
  **only the data the cleanup needs**, the same idea as option (e).
- concurrent.futures (https://github.com/python/cpython/blob/main/Lib/concurrent/futures/thread.py): a
  `WeakKeyDictionary` of threads plus one `_python_exit` via `threading._register_atexit`. multiprocessing `util.Finalize`
  (https://github.com/python/cpython/blob/main/Lib/multiprocessing/util.py): weakref to obj, module registry, one
  `_exit_function`. logging (https://github.com/python/cpython/blob/main/Lib/logging/__init__.py): `_handlerList` of
  weakrefs, one module-level `atexit.register(shutdown)`. In all three, one hook iterates weak state. That works because
  their objects need no cleanup once collected, which is not true here (a cluster claim outlives the client).
- kubernetes and kubernetes_asyncio `ApiClient` (installed `api_client.py`): `atexit.register(self.close)` on lazy pool
  creation, `atexit.unregister(self.close)` in `close()`. This is option (d), and it works there because there is one
  lazy creation point.
- requests `Session`, httpx `Client`: no atexit, no `__del__`. aiohttp `ClientSession`: no atexit, `__del__` only warns
  `ResourceWarning`. asyncio loops: no atexit, warning on unclosed loops. Network libraries leave local resources to the
  OS. This SDK is different because the resource lives remotely.

## Options (async; the sync column is the same unless noted)

| option | dropped never-closed client: sandboxes deleted at exit? | reuse after close | new state | code | failure modes |
|---|---|---|---|---|---|
| e. module fn + data args (strong) | yes (same as today) | covered | none | ~5 lines + test rewiring | about 480 B left per client (linear but 17x smaller); breaks if the dict is ever rebound (add a comment) |
| a. unregister in `close()` if registry empty | yes | **lost** for later sandboxes | none | ~3 | silent claim leak on reuse |
| d. (a) + re-register on next insert | yes | covered | flag | ~10, checked at every insert (create, get, attach) under `_lock` | missed insert path means leak; races with `close()` |
| b. weakref hook / WeakSet at exit | **no**: the client is freed with its registry, so `AsyncSandboxClient(cfg).create_sandbox()` loses exit cleanup and its port-forwards | covered | weakref | ~8 | claim leak in the cluster (worse than memory) |
| b'. `weakref.finalize` running the cleanup at GC | deleted **at GC**, which is earlier | covered | finalize | ~5 | deletes a sandbox the caller is still using (handle alive, client dropped). Wrong. |
| c. document "reuse one client" | yes | covered | none | docs | 1.4-8 KB traced per client remains |
| f. (e) + one module registry, finalize drops empty entries | yes | covered | module dict + finalize | ~20 | zero residual, but the most code; only worth it if 480 B/client matters |

Async cleanup at GC needs no event loop (the existing exit path is synchronous), so b' is wrong only because it fires at
the wrong time, not because it cannot work.

## Sync

Same bug, opt-in only. Option (e) for sync: a module function taking `(registry, explicit)` that terminates
non-explicit tracked handles, pops them and closes explicit ones. `__exit__` calls the same function. Its tracked branch
duplicates `delete_sandbox`'s terminate, pop and log steps, so move that branch into one helper that both use (rule in
one place). On main, every key at exit is tracked, so `delete_sandbox`'s untracked `_delete_claim` branch is never used
by cleanup (verified `:401-411`, `:372-378`). **Unregistering in `__exit__` is wrong:** sync `__exit__` closes nothing and
the client is documented as reusable, so sandboxes created after the block would lose exit cleanup.

## Recommendation

- Async: **(e)**. Sync: **(e)** with the shared helper. **One PR**: same issue, same pattern, and it keeps parity.
  Tests: call the module function directly and assert a created-then-closed client is freed (`weakref` + `gc.collect()`).
  The current tests that call `client._atexit_cleanup()` move to the function. That is test churn, not a production
  change made for a test.
- What would make it wrong: (1) Brian wants zero per-client growth (then f, or c with `cleanup=False` guidance);
  (2) a future change rebinds the registry dict (comment at the assignment); (3) upstream decides a dropped client
  should *not* clean up at exit (then b becomes acceptable, but that contract change needs an issue first).

## Comparison with the task's options (G3-memleak-fix2.md, PR 3)

- Facts agree: registration lines, reusable `close()`, failed-close retention, sync has no `close()`, all clients alive.
- The task missed (e). That changes the trade-off: its (a) costs a flag *and* still risks reuse leaks, and its (c) is not
  free (8 KB traced per used client is the bigger part).
- Its (b) cost is right but understated: on main (no PR 1), live handles do **not** keep the client alive, so
  `client.create_sandbox()` followed by dropping the client loses exit cleanup even while the handle is in use.
- Its (a) bullet says "about 5 lines plus a flag". That is really (d), and the flag must be checked on every insert path.
- I agree that sync `__exit__` unregistering changes behavior on reuse. I would reject it rather than offer it.
- Its measurement (22 KiB incl. malloc, Py 3.12) and mine (8 KB traced, Py 3.13) are consistent.
