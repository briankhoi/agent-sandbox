# G2 memleak fix: decision needed on freeing closed handles

Author: G2 task conversation, 2026-10-22. Asks the coordinator (or Brian) to pick an option before the fix branch changes again.

## Where things stand

- Fix branch `fix/python-sdk-release-terminated-handles` at `2e02e420` (base upstream/main `840fdfde`), pushed to `origin`. It implements option (a): at each insert (`create_sandbox`, `get_sandbox`) the client drops handles whose `claim_name is None`, the marker `terminate()` sets only after a successful delete. Both shells, tests mutation-checked, mypy clean, and a fresh reviewer found no blocking issues.
- Measured on `par-mem` (600 rollouts per arm, same shape as `6v2-baseline-long-async`, host now 24 CPUs and 94 GiB, both arms started at 120 Ready, 0 errors):
  - upstream main grows 0.63 MiB of `uordblks` per rollout (375 MiB over 600).
  - the fix still grows 0.36 MiB per rollout (218 MiB over 600), with visible drops. The success criteria fail (second-half slope ratio 0.35, target at most 0.10; `uordblks` growth 218 MiB, target at most 50).
- Cause, measured in the driver pod with real `AsyncSandbox` handles and the GC turned off: a closed, dropped handle still holds 0.690 MiB, and `gc.collect()` then finds 213 cyclic objects per handle. The handle passes its bound methods (`get_pod_ip`, `get_pod_name`, `get_service_fqdn`) to its connector (`async_connector.py:149-150`) and to the sandboxd strategy (`:843-844`, pod tunnel `:664`), so handle and connector form a reference cycle. Dropping the handle from the dict therefore frees it only at the next full cyclic GC. That GC triggers on object counts, not bytes, so it can't see the 0.7 MiB of OpenSSL memory behind each handle.
- Same probe with two candidate edits, applied after `close_connection()`:
  - callbacks cleared on the connector and its strategy: 0.006 MiB left per dropped handle (177 small cyclic objects remain)
  - `connector.client` set to None: 0.006 MiB left (188 small cyclic objects remain)
- Running now: 2,400-rollout arms (`6v2-leakbase-long-async`, `6v2-leakfix-long-async`) to show whether option (a) alone levels off under the GC.

Scripts: `test/benchmarks/batch-claim/results/leakfix/handle_cycle.py` and `handle_cycle_edges.py` on `bench/async-mem-probe`. That branch has unpushed commits, which will be pushed with the report.

## The question

Option (a) bounds the registry. The memory of a dropped handle is still held until a full cyclic GC. Should the fix also free it promptly, and if so how?

## Options

There are two layers: bounding the registry, which (a) already does, and freeing a dropped handle's native memory promptly, which (a) cannot do because of the cycle.

### (g) Rely on the GC (keep (a) only)
- What: no further change.
- New state or type change: none.
- Risk: memory is a sawtooth whose peak depends on GC heuristics, not bytes. The 2,400-rollout runs will show whether it levels off and where.
- Cost: none.

### (h) Call `gc.collect()` from the client
- What: on terminate, or every N inserts.
- New state or type change: none.
- Risk: library-wide pauses and a global side effect. Widely seen as an anti-pattern for libraries.
- Cost: small.

### (i) Clear the callbacks at close (recommended)
- What: `AsyncSandboxConnector.close()` and each strategy's `close()` set `_get_pod_ip`, `_get_pod_name` and `_get_service_fqdn` to None. Same in the sync shell.
- New state or type change: none. The attributes are already `Optional`.
- Risk: low. A closed connector refuses all work (`_ensure_open`, `async_connector.py:481-484`) and never reopens, so nothing calls the callbacks after close.
- Cost: about three small edits per shell, plus one test per shell that a closed, dropped handle is freed with the GC off.

### (f) Drop the httpx client at close
- What: `self.client = None` after `aclose()`.
- New state or type change: yes. `client` becomes `Optional`, and it is a public-looking attribute.
- Risk: low functionally (the client is unusable after `aclose()`), but every `self.client.` use needs a None guard for mypy. Async shell only, since the sync connector holds little (see below).
- Cost: small, but it widens a type.

### (e) Weak references for the callbacks (`weakref.WeakMethod`)
- What: the connector holds weak references to the handle's methods.
- New state or type change: no new field, but a new pattern (the SDK uses no weakrefs today).
- Risk: real failure mode. `cmds = sandbox.commands; del sandbox; await cmds.run(...)` lets the handle be collected, and the connector's `get_pod_ip` silently goes dead.
- Cost: moderate and subtle.

### (d) One SSL context per client, or a lazily built httpx client
- What: share one SSL context across a client's handles, or create the httpx client on first HTTP use. In sandboxd mode commands go over gRPC, so building a CA-loaded SSL context per handle is pure waste.
- New state or type change: possibly a shared-context field.
- Risk: low, but it changes how TLS config is shared, so Direct configs with `ca_cert` need care.
- Cost: larger. Better as a separate PR. It also lowers the cost of live handles (40 live handles × 0.7 MiB is about 28 MiB).

### (j) The handle drops `self.connector` at close
- What: break every edge from the handle side.
- New state or type change: yes, `connector` becomes `Optional`.
- Risk: `_close_for_atexit` (`async_sandbox.py:208`) and other paths read `self.connector` after close.
- Not worth it.

## Best practices that apply

- An object that owns native or OS resources should release them deterministically in `close()` or `aclose()`, not leave them to the GC. The GC counts objects, not bytes.
- Avoid passing bound methods into an owned object without teardown: that creates exactly this cycle.
- Remove registry entries on terminal state, which is what (a) does.
- httpx recommends reusing a client rather than creating many.

## Recommendation

1. Keep (a). It stops the registry from growing without bound.
2. Add (i) to the same PR. It frees the 0.7 MiB at drop time with no new state and no type change, and it rests on an existing invariant: a closed connector never reopens.
3. File (d) as a separate follow-up issue.

Avoid (e), (h) and (j). Take (f) only if one edit is preferred over the strategy edits in (i), accepting the type widening.

Sync shell: the same cycle pattern exists (`connector.py:401, 532-533, 596, 647`), but a closed sync handle holds 0.058 MiB (measured), so the sync change in (i) is for parity, not for memory.

## If (i) is chosen

- Implement it in both shells with identical comments.
- Add one owning test per shell: a closed, dropped handle is freed by reference counting with the GC off.
- Mutation-check the test.
- Re-run the 600 and 2,400-rollout arms against the new fix SHA, then finish the G2 report.
