# PR 1 hook vs PR 3 exit hook: does a tracked handle pin the client?

Independent audit, 2026-10-26. PR 1 `c2c006ee`, PR 2 `b3574fa9`, base upstream/main `96ef188e`. Read-only on code; scratch in
`/tmp/par-audit4` (`demo.py`, `apply.py`, worktrees `wt` = PR1+PR2, `wt2` = PR1+PR2 + the proposed hook change).

## One-minute summary

**I agree with the proposed change.** The interaction is real (measured): with PR 1 as is plus a PR 3 data-only exit hook, a
dropped client that still has a tracked handle stays alive until exit, sync and async, with or without gc. With the hook
bound to the registry instead, the client is freed by refcount alone in every case. PR 1 alone also adds a client <-> handle
reference cycle (client -> registry -> handle -> bound `_untrack` -> client), so even with no exit hook a dropped client needs
the cyclic GC, which is what PR 2 removes elsewhere. **Recommended form (smallest diff on PR 1):** keep `_untrack` in the
client, make it a `@staticmethod` taking `(registry, sandbox)`, and set
`handle._on_terminated = functools.partial(self._untrack, self._active_connection_sandboxes)` at both insert sites in both
clients. Behavior is unchanged (all 737 unit tests pass on both variants; mypy unchanged). Put the change in PR 1, which owns
the hook. Nothing else on a handle points at the client.

## 1. Is the interaction real? (measured, `/tmp/par-audit4/demo.py`, Py 3.13.15, k8s config mocked)

PR 3 is simulated as a module list holding `(registry, explicit_claims, api_client)`, i.e. option (e). `gc.disable()`
during each case; "nogc / gc" = client still alive before / after `gc.collect()`.

| case (after `del client`) | PR1 as is: nogc / gc | proposed: nogc / gc |
|---|---|---|
| sync, handle kept by caller, registry holds it | alive / alive | freed / freed |
| sync, handle dropped too, registry holds it | alive / alive | freed / freed |
| sync, handle terminated (registry empty) | freed / freed | freed / freed |
| async, handle kept / dropped, no `close()` | alive / alive | freed / freed |
| async, `close()` succeeded (registry empty) | freed / freed | freed / freed |
| async, handle terminated | freed / freed | freed / freed |
| no exit hook at all, sync, client+handle dropped | alive / freed (cycle) | freed / freed |

So the claim holds, with a nuance: PR 3 is defeated only while the registry is non-empty. Async clients that `close()`
cleanly are not affected; dropped-without-close async clients, and sync `cleanup=True` clients after `__exit__` (explicit
claims stay tracked, closed, verified `sandbox_client.py:413-422`) are.

## 2. Alternatives (simplest first)

| option | cost / failure mode | verdict |
|---|---|---|
| **staticmethod `_untrack(registry, sandbox)` + `partial`** | ~6 lines per client; a comment must say why it is static | **chosen**: same location and name as PR 1, keeps class-level override via `self._untrack` lookup |
| module-level fn + `partial` over the registry | same graph; moves the logic out of the class (one per module for parity, or a shared helper) | equal; fine if reviewers prefer no staticmethod |
| module fn + dict stored on the handle | new handle attribute (state), handle learns the registry | rejected: more state for the same result |
| handle holds the registry and pops itself | handle learns the key format and the identity rule | rejected: moves client logic into the handle |
| `weakref.WeakMethod(client._untrack)` | once the client is dropped (PR 3 keeps the registry alive), terminate no longer untracks: the handle stays pinned to exit and exit cleanup re-deletes the claim | rejected: reintroduces PR 1's leak |
| staticmethod without `partial` | handle still needs the registry from somewhere | not a complete option |
| leave PR 1 as is | client pinned while any handle is tracked; extra client<->handle cycle | rejected: defeats PR 3 for those clients |

Checks on the chosen form (verified unless marked):
- Typing: `registry: dict[tuple[str, str], T]` in the static method; mypy 1.20 `--python-version 3.11` with the repo config
  reports only the same 9 pre-existing `_grpc_auth.py` errors (grpc missing in my venv) on both trees (measured). The
  `Callable[[Sandbox], None] | None` attribute (`sandbox.py:47`, `async_sandbox.py:48`) needs no change.
- Async lock: `_untrack` takes no lock today (`async_sandbox_client.py:486-494`) and is called from `terminate()` on the loop
  thread; unchanged. Sync client has no lock. Sync/async parity: identical edit in both.
- Registry replaced: the partial binds the dict the handle was inserted into. Production never rebinds it (verified grep);
  tests rebind it only before creating handles (`test_lifecycle_integration.py:64,112,144`) or with mocks
  (`test_async_sandboxclient.py:318-598`); all pass (measured). If a dict were rebound later, the old bound method looked
  in the new dict, the partial in the old one; the partial matches where the handle actually lives (inferred).
- Subclasses: no subclass overrides `_untrack` (verified grep). A future subclass overriding it as an instance method
  would re-pin the client (inferred); the comment should say so.

## 3. Other references to the client after PR 1+2+3 (verified)

- Handle attributes: `k8s_helper` (shared helper; holds only the ApiClient, `k8s_helper.py:59`, `async_k8s_helper.py:54-58`),
  `connection_config`, `tracer_config`, own tracer (`sandbox.py:61-87`). None reference the client.
- Connector callbacks are bound methods of the *handle* (`sandbox.py:63-71`), released by PR 2; not the client.
- `SnapshotEngine(get_pod_name_func=self.get_pod_name)` (`sandbox_with_snapshot_support.py:87-90`) is a handle-to-handle
  cycle, not the client. `PodSnapshotSandboxClient` (`podsnapshot_client.py:25-41`) and `ComputerUseSandboxClient`
  (`computer_use.py:35-39`) add no hooks or closures.
- Only pins: the exit hook (`sandbox_client.py:106`, `async_sandbox_client.py:203`, fixed by PR 3) and `_on_terminated`
  (`sandbox_client.py:261,325`, `async_sandbox_client.py:377,474`, fixed by this change).

## 4. Recommendation and tests

PR order: PR 3 is independent code (only the registration) but its "client with a tracked handle is freed" test needs this
change, so stack PR 3 on PR 1 (or land PR 1 first).

| test | owner |
|---|---|
| (a) terminate removes the registry entry; replaced handle kept; failed terminate stays tracked (exist: `test_adoption_lifecycle.py:564-597`) | PR 1 |
| (a') client (`cleanup=False`) with a tracked handle, caller keeps the handle, `del client` -> weakref dead with gc disabled. Fails on PR 1 today (measured row 1/last row) | PR 1 |
| (b) closed handle freed without gc (exist in PR 2's `test_sandbox.py` / `test_async_sandbox.py`) | PR 2 |
| (c) default `cleanup=True`, closed-and-dropped client freed with an empty registry and with a tracked handle; gc disabled | PR 3 |
| (d) after `del client`, calling the registered exit function still deletes tracked non-explicit claims and closes explicit ones | PR 3 |

PR 3 must also update `test_adoption_lifecycle.py:584` (`register.call_args.args[0]()` calls the hook with no args).

What would make this wrong: (1) a reviewer wants handles to keep their client alive on purpose (no code relies on it:
handles never reach the client except via the hook); (2) the registry dict becomes rebindable (then partial and exit hook
both need a stable container); (3) upstream rejects PR 3, in which case the change is still worth it to remove the cycle,
but its motivation shrinks to "free without the cyclic GC".
