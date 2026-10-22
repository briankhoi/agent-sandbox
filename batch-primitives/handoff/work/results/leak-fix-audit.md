# Independent audit: terminated-handle leak in the Python SDK

Auditor: independent subagent, 2026-10-22. Code read at upstream/main `840fdfde`. Paths are relative to `clients/python/agentic-sandbox-client/k8s_agent_sandbox/`. Read-only; no branch was touched.

## Summary (one minute)

- There are two separate defects, and fixing only the first does not make memory flat. **(1) Registry:** `terminate()` never removes its own entry, so the client keeps every terminated handle. **(2) Cycle:** each handle and its connector form a reference cycle, so even a handle that nobody references is freed only by a full cyclic GC, which counts objects, not the ~0.68 MiB of OpenSSL memory each one holds.
- Measured with a window of 64 live async handles and 2,400 churned: eager removal alone grows the malloc'd heap by 311 -> 376 -> 417 -> 459 MiB at the quartiles (peak +543 MiB). Eager removal plus a broken cycle stays flat at +44 to 45 MiB, which is just the live set.
- **Recommendation:** two small upstream PRs, in this order. PR 1: `terminate()` calls a client hook after a successful delete, and the hook removes the entry only if it is still that same handle (TaskGroup's `add_done_callback` pattern). This replaces option (a). PR 2: drop the handle-to-connector callback edges at final close (the G2 note's option (i)).
- On "1k callbacks at once": yes, one reference per live handle. A bound method is 73 B (measured), so 1,000 of them are 71 KiB, against 683 MiB for 1,000 async handles (about 0.01%). Each callback is freed together with its handle. It can also be a single shared object, which costs an 8-byte slot per handle.

## Ranked findings

1. **Strong references are right for live handles and wrong for terminated ones** (verified). The dict supports four things: `close()` closing connections the user never closed (`async_sandbox_client.py:214-228`), atexit killing port-forward subprocesses through `_close_for_atexit` and deleting claims (`:523-592`), reuse in `get_sandbox` and in create retries, which keeps extension state (`:345-349`, `:449-450`), and `delete_all` (`:505-514`). A terminated handle needs none of these, because its claim is deleted and its connection is closed. *Recommendation:* keep the strong dict and remove entries on successful terminate. *Reason:* weak references would orphan the port-forward processes and unclosed connections of handles the user dropped, and the cycle would make entries disappear at nondeterministic times.
2. **Eager removal through a hook called by `terminate()`** (proposal). Mechanism: the handle gets a class attribute `_on_terminated: Callable[[Self], None] | None = None`. The client sets `sandbox._on_terminated = self._untrack` at both construction sites. `terminate()` calls it after the delete succeeds and before it clears `claim_name`. `_untrack(h)`: `key = (h.namespace, h.claim_name); if self._active_connection_sandboxes.get(key) is h: del ...`. *Why new state:* the handle holds no reference to the client today (`async_sandbox.py:45-92`), and existing data only allows pruning on the next client call, which is (a), and that fails Brian's case of 1,000 terminations followed by no more creates. *Why an attribute and not a constructor kwarg:* the client builds `self.sandbox_class(...)`, and a third-party subclass whose `__init__` has a fixed signature would raise `TypeError`. In-repo subclasses forward `**kwargs` (`gke_extensions/snapshots/sandbox_with_snapshot_support.py:85-86`). A subclass whose `terminate()` doesn't call `super()` keeps today's behavior; nothing breaks.
3. **The cycle is the bigger memory term once the registry is fixed** (measured above, and consistent with the G2 note's 394 MiB peak). It also affects users who never touch the registry, for example anyone who drops an unclosed or closed handle. *Recommendation:* break it at final close: clear `_get_pod_ip`, `_get_pod_name` and `_get_service_fqdn` on the connector and strategies (`async_connector.py:149-150, 664, 843-844`). These fields are already `Optional`, so no type changes. On the async side, connector `close()` is final. On the sync side, connector `close()` doubles as a reset, so clear the edges in `Sandbox.close_connection()` instead. That is final for a handle, because `get_sandbox` replaces closed handles (`sandbox_client.py:299-317`). Leaving this to the GC is a valid but weaker choice (see table).
4. **A bug that PR 1 also fixes:** async atexit issues one DELETE per terminated key that is still tracked. It loops over the keys, not over `handle.claim_name` (`async_sandbox_client.py:571`), each DELETE runs serially with a 300 s timeout (`:53`), and a 404 counts as success. After 2,400 rollouts that is 2,400 serial DELETEs at exit (inferred from the code; not run).
5. **Redundant pop:** `delete_sandbox` pops the entry after `terminate()` (`async_sandbox_client.py:494-497`, `sandbox_client.py:366-368`). With the hook, that pop expresses the same rule a second time. Remove it, so the rule lives in one place.
6. **Out of scope, flag separately:** `list_active_sandboxes` drops closed handles whose claims are still alive (`async_sandbox_client.py:469-475`, `sandbox_client.py:330-333`), so atexit and `delete_all` never delete those claims. Also, `atexit.register(self._atexit_cleanup)` keeps every async client alive until exit (`:198`), which leaks clients for programs that create many of them.

## Alternatives (simplest first)

| Option | Memory / speed | Code | Failure modes / API | Verdict |
|---|---|---|---|---|
| (a) prune `claim_name is None` on insert | bounded only while you keep creating; holds N handles after the last create | ~6 lines/shell, no new state | Brian's 1k case keeps 683 MiB until `close()`. CPython `subprocess._cleanup` uses the same lazy pattern ([subprocess.py](https://github.com/python/cpython/blob/3.12/Lib/subprocess.py)) | rejected |
| **Hook set by the client, called by `terminate()`** | 73 B per handle, removal O(1) (1k removals took 397 µs, measured) | ~12 lines/shell, one private attribute | handle keeps the client alive (minor; the async client is already pinned by atexit); a race on the sync side (below) | **recommended** |
| hook as a constructor kwarg | same | same | breaks third-party subclasses with a fixed `__init__` | rejected |
| handle holds the client (or `weakref.ref(client)`) | same | similar | wider coupling; the weakref adds a pattern the SDK doesn't use | no gain over the hook |
| route terminate through the client | none | small | users call `sandbox.terminate()` directly, and that path would still leak | rejected |
| `WeakValueDictionary` + a set of names | depends on GC | +1 structure, rewrites reuse/close/atexit | orphaned port-forwards and unclosed connections; nondeterministic reuse | rejected |
| track claim names only | smallest | rewrites `close()`/atexit | can't close connections or kill port-forwards | rejected |
| `weakref.finalize` on the handle | n/a | small | never fires while the dict holds the handle; doesn't address the cause | rejected |
| cycle: leave it to the GC | +311 to 459 MiB, still rising at 2,400 (window 64) | 0 | peak set by GC heuristics | weaker |
| cycle: `gc.collect()` in the library | pauses | small | global side effect; an anti-pattern for libraries | rejected |
| cycle: clear callbacks at final close | flat at the live set (+44 MiB) | ~3 edits/shell | none known; a closed connector refuses work | **recommended (PR 2)** |
| cycle: pass the helper and names instead of bound methods | flat | larger refactor of the connector and strategies | API churn in internals | later, if ever |

Precedents (verified in the CPython 3.13 sources on this host; same code at the 3.12 URLs): `asyncio.TaskGroup` registers `task.add_done_callback(self._on_task_done)`, which calls `self._tasks.discard(task)`, one bound method per task ([taskgroups.py](https://github.com/python/cpython/blob/3.12/Lib/asyncio/taskgroups.py) L205, L228-229). The module-wide registries (`asyncio._scheduled_tasks`, a `WeakSet`; `concurrent.futures.thread._threads_queues`, a `WeakKeyDictionary`; logging's `_handlerList` of weakrefs) are weak only because they don't own cleanup ([tasks.py](https://github.com/python/cpython/blob/3.12/Lib/asyncio/tasks.py), [thread.py](https://github.com/python/cpython/blob/3.12/Lib/concurrent/futures/thread.py), [logging](https://github.com/python/cpython/blob/3.12/Lib/logging/__init__.py), [weakref.finalize docs](https://docs.python.org/3/library/weakref.html#finalizer-objects)). Our client does own cleanup, so the TaskGroup pattern fits. I did not check httpx, aiohttp, docker-py, boto3 or the kubernetes client, so I make no claim about them.

## Edge cases (any fix must handle these)

- **Delete fails:** the hook runs only after a successful delete, so the entry stays and atexit and `delete_all` retry it. A 404 counts as success.
- **`get_sandbox` replaced the handle:** the identity check (`is h`) stops an old handle from evicting its replacement. The replacement stays tracked for a deleted claim; atexit then sends one 404 DELETE, same as main.
- **`close_connection()` without terminate:** stays tracked, because the claim is still alive. Correct.
- **`delete_sandbox` vs `terminate`:** both go through `terminate()`, so the explicit pop goes away (finding 5). Without a handle, `delete_sandbox` deletes the claim directly, as it does today.
- **Async concurrency:** the hook is synchronous and contains no `await`, so it is atomic on the loop. It must not take `self._lock`, which is an asyncio lock. Every iteration over the dict uses a `list(...)` snapshot (`:221, :508, :518, :536`), so removal while another coroutine iterates is safe.
- **Sync threads:** the get-then-del pair isn't atomic. The worst case is that a concurrent re-attach to the same claim being deleted loses its entry. The sync client has no locking anywhere today, so this is not a new guarantee being broken.
- **atexit:** unaffected. Terminated handles are already gone, which removes the redundant DELETEs from finding 4.

## Measurements (Python 3.13.15, /tmp/par-audit, real `AsyncSandbox` and `Sandbox` with a fake helper and `SandboxDirectConnectionConfig`, malloc `uordblks`)

| What | Result |
|---|---|
| 1k bound methods / partials / closures | 73 B / 366 B / 366 B each (71 / 357 / 357 KiB) |
| 1k `AsyncSandbox` / 1k sync `Sandbox` | 682.7 MiB / 0.3 MiB (the sync figure differs from the 0.058 MiB in the brief; the config probably differs) |
| mass terminate of 1k with eager removal | 0.13 s, no spike (709 -> 709 MiB). Still 709 MiB after the user drops them, because of the cycle; `gc.collect()` brings it to 27 MiB |
| 2,400 churn, short-lived handles, eager removal | +21/27/33/39 MiB; with the cycle broken +0 |
| 2,400 churn, 64 live handles, eager removal | +311/376/417/459 MiB, peak +543; with the cycle broken +44/44/44/45 |

Scripts: `/tmp/par-audit/measure.py` and `/tmp/par-audit/churn_window.py` (scratch, not committed). In the scripts, the cycle is broken with `h.connector = None` as a stand-in. The G2 note measured the real candidate edit, clearing the callbacks, at 0.006 MiB left per dropped handle.

## Proposed change set

- **PR 1 (registry), both shells:** `sandbox.py` and `async_sandbox.py` get a class attribute and the hook call in `terminate()` (~4 lines each). The two clients each get `_untrack` (~5 lines), an assignment at the two construction sites, and lose the pop in `delete_sandbox`. About 30 lines total. Tests, per shell: (t1) terminate removes its own entry, and a stale handle's terminate doesn't remove its replacement; (t2) a failed delete keeps the entry. Mutation checks: removing the call fails t1, dropping the `is` check fails t1, and moving the call before the delete fails t2.
- **PR 2 (cycle), both shells:** clear the callbacks at final close (~3 edits per shell). Test, per shell: with `gc.disable()`, a closed handle is freed on `del` (a `weakref.ref` goes dead). Mutation: removing the clearing fails it.
- Why two PRs: each is one concern (who owns the entry vs when the memory is released), each can be reviewed alone, and PR 2 helps users who never use the registry. Upstream squash-merges, so one PR would hide that split.

## What would make this wrong

- If some caller relies on a terminated handle still appearing in `list_active_sandboxes` or being returned by `get_sandbox`. `is_active` is already False after terminate, so neither does that today (verified).
- If real handles in the driver are dominated by something other than the SSL context, or a later Python GC (for example 3.14's incremental collector) collects these cycles promptly. That would weaken the case for PR 2, not PR 1.
- If a strategy calls a callback after close. The G2 note says a closed connector refuses all work (`async_connector.py:481-484`); I did not re-verify every strategy.

## Questions for Brian

1. Hook as a private attribute set by the client (recommended), or as a constructor kwarg (cleaner typing, but it breaks fixed-signature subclasses)?
2. Two PRs (recommended), or one?
3. Should findings 6 (`list_active_sandboxes` drops alive claims, and atexit pins every client) be filed as separate issues?
